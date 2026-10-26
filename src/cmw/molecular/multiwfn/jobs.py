"""Read-only engine checks and intentional preparation for bounded Jobs profiles.

The established foreground workflows retain launch, signal and finalizer ownership.
A prepared plan fixes their scientific attempt before Jobs registers observation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import stat
from uuid import uuid4

from cmw.core.provenance import atomic_write_json, file_hash, read_json, stable_hash
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file
from cmw.molecular.stacking.hole_electron import FragmentDefinition
from cmw.molecular.workflows.source import validate_source_result, _validated_artifact
from cmw.molecular.workflows.multiwfn_analysis import AnalysisOperation, plan_analysis, prepare_analysis, write_menu
from cmw.structure.xyz import read_xyz
from .adapter import RUNNER_EVENT_PREDECESSORS
from .automation import Operation, menu_stream
from .excited_states import Multiwfn2026IfctRenderer
from .runtime import MENU_CONTRACT, MultiwfnRuntime, prepare_settings, render_settings, resolve_menu_contract, resolve_threads

PROFILES = {'FMO', 'ESP', 'IGMH', 'IFCT'}
EVENTS = frozenset(RUNNER_EVENT_PREDECESSORS)
MAX_EVENT_BYTES = 64 * 1024


class Unsupported(ValueError):
    pass


class Unassessed(ValueError):
    pass


def _absolute(value, name):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f'{name} must be an absolute path')
    path = Path(value)
    if '\x00' in value or '\n' in value or '\r' in value:
        raise ValueError(f'{name} contains an unsafe path character')
    return path.resolve()


def _dependency(path):
    from cmw.jobs.runner_assets import file_identity
    return file_identity(path)


def _runtime(spec, dependencies):
    runtime = spec.get('runtime', {})
    allowed = {'qualification', 'executable', 'sha256', 'version', 'settings_source', 'settings_sha256', 'threads', 'menu_contract', 'contract'}
    if not isinstance(runtime, dict) or set(runtime) - allowed:
        raise Unsupported('Unknown Multiwfn runtime fields; arbitrary command/menu execution is not supported')
    if runtime.get('qualification') not in {'recorded', 'synthetic'}:
        raise Unassessed('Runtime qualification must identify recorded evidence or a synthetic test executable')
    executable = _absolute(runtime.get('executable'), 'runtime.executable')
    settings = _absolute(runtime.get('settings_source'), 'runtime.settings_source')
    for path, field in ((executable, 'sha256'), (settings, 'settings_sha256')):
        identity = _dependency(path)
        if identity['sha256'] != runtime.get(field):
            raise ValueError(f'Runtime dependency identity differs: {field}')
        dependencies.append(identity)
    if not os.access(executable, os.X_OK):
        raise ValueError('Multiwfn executable is not executable')
    version = runtime.get('version')
    if not isinstance(version, str):
        raise Unassessed('Recorded Multiwfn version is unavailable; static checks never probe the engine')
    try:
        contract = resolve_menu_contract(version)
    except ValueError as exc:
        raise Unsupported(str(exc)) from exc
    if runtime.get('menu_contract') != contract:
        raise Unsupported('Unknown Multiwfn version/menu contract combination')
    threads = resolve_threads(cli_value=runtime.get('threads'), environment={})
    if runtime.get('threads') is None:
        raise ValueError('An explicit thread count is required; the legacy eight-thread default is not a Jobs commitment')
    resources = spec.get('resources', {})
    if resources.get('cpus') != threads or resources.get('mpi_ranks', 1) != 1 or resources.get('threads_per_rank', threads) != threads:
        raise ValueError('Multiwfn threads must equal Jobs CPUs, with one process and matching threads_per_rank')
    memory = resources.get('memory_gib')
    if isinstance(memory, bool) or not isinstance(memory, (int, float)) or not math.isfinite(memory) or memory <= 0:
        raise ValueError('A finite positive total memory commitment in GiB is required')
    # The exact attempt-local derivation is reviewed now, without writing it.
    if settings.stat().st_size > 1024**2:
        raise Unsupported('Settings input exceeds the 1 MiB static-inspection bound')
    effective = render_settings(settings.read_bytes(), threads)
    if runtime['qualification'] == 'recorded':
        evidence_path = _absolute(runtime.get('contract'), 'runtime.contract')
        dependencies.append(_dependency(evidence_path))
        if evidence_path.stat().st_size > 1024**2:
            raise Unsupported('Recorded runtime evidence exceeds the 1 MiB metadata bound')
        evidence = read_json(evidence_path)
        if evidence.get('validation', {}).get('status') != 'PASSED':
            raise Unassessed('Recorded Multiwfn runtime contract is not PASSED')
        for key, expected in {'executable': str(executable), 'executable_sha256': runtime['sha256'],
                              'version': version, 'menu_contract': MENU_CONTRACT,
                              'settings_source': str(settings), 'settings_source_sha256': runtime['settings_sha256'],
                              'requested_nthreads': threads}.items():
            if evidence.get(key) != expected:
                raise ValueError(f'Recorded runtime contract differs from selected {key}')
    return {'executable': str(executable), 'executable_sha256': runtime['sha256'],
            'version': version, 'menu_contract': contract, 'requested_nthreads': threads,
            'settings_source': str(settings), 'settings_source_sha256': runtime['settings_sha256'],
            'settings_sha256': hashlib.sha256(effective).hexdigest(), 'qualification': runtime['qualification']}


def _ifct(science, source, runtime, attempt, dependencies):
    if runtime['version'] != '2026.7.15':
        raise Unsupported('IFCT requires the exact Multiwfn 2026.7.15 grammar')
    if type(science.get('state_index')) is not int or science['state_index'] < 1:
        raise ValueError('IFCT state_index must be an exact positive integer')
    if type(science.get('atom_count')) is not int or science['atom_count'] < 1:
        raise ValueError('IFCT atom_count must be an exact positive integer')
    fragments_value = science.get('fragments')
    if not isinstance(fragments_value, list) or len(fragments_value) != 2:
        raise ValueError('IFCT requires exactly two explicit fragments')
    for fragment in fragments_value:
        if not isinstance(fragment, dict) or set(fragment) != {'label', 'indices_zero_based'}:
            raise ValueError('IFCT fragments require label and indices_zero_based only')
        indices = fragment['indices_zero_based']
        if (not isinstance(indices, list) or not indices
                or any(type(index) is not int or index < 0 for index in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError('IFCT zero-based indices must be distinct exact nonnegative integers')
    geometry = read_xyz(Path(source.geometry_path))
    if science.get('atom_count') != geometry.atom_count:
        raise ValueError('IFCT atom count differs from the validated ordered geometry')
    output = _absolute(science.get('orca_output'), 'scientific.orca_output')
    record_path = Path(source.result_path)
    record = read_json(record_path)
    # A conversion preserves the parent result identity; it does not relocate it.
    provenance = record.get('provenance', {})
    if provenance.get('operation') == 'orca_gbw_to_molden':
        parent = _absolute(provenance.get('source_result'), 'conversion source result')
        identity = _dependency(parent)
        if identity['sha256'] != provenance.get('source_result_sha256'):
            raise ValueError('Converted wavefunction source-result provenance changed')
        dependencies.append(identity)
        record = read_json(parent)
        record_path = parent
        if record.get('target', {}).get('target_id') != provenance.get('source_target_id') or record.get('attempt', {}).get('attempt_id') != provenance.get('source_attempt_id'):
            raise ValueError('Converted wavefunction source target/attempt identity differs')
    if record.get('execution', {}).get('status') != 'SUCCESS' or record.get('scientific', {}).get('status') != 'VALID' or not record.get('attempt', {}).get('attempt_id'):
        raise ValueError('IFCT requires a finalized source result and exact source attempt')
    declared, digest = _validated_artifact(record_path, record.get('artifacts', {}), 'output', record)
    if declared != output:
        raise ValueError('IFCT ORCA output is not the authoritative source-result output artifact')
    dependencies.append(_dependency(output))
    if output.stat().st_size > 64 * 1024**2:
        raise Unsupported('IFCT ORCA state evidence exceeds the 64 MiB bounded terminal-parser input')
    native = parse_orca_tda_excited_states_file(output)
    state = native.state(science['spin_manifold'], science['state_index'])
    fragments = tuple(FragmentDefinition(f['label'], tuple(f['indices_zero_based'])) for f in science['fragments'])
    ifct_spec = {'orca_output': str(output), 'wavefunction': source.wavefunction_path,
                 'geometry': source.geometry_path, 'spin_manifold': science['spin_manifold'],
                 'state_index': science['state_index'], 'atom_count': geometry.atom_count,
                 'fragments': science['fragments']}
    # The renderer reads settings identity. During pure review the source file is
    # the available immutable identity; preparation replaces it with the derived one.
    preview_runtime = dict(runtime, settings_path=runtime['settings_source'],
                           settings_sha256=runtime['settings_source_sha256'])
    rendered = Multiwfn2026IfctRenderer().render(
        state, orca_output_path=output, source_wavefunction_path=Path(source.wavefunction_path),
        fragment_definitions=fragments, atom_count=geometry.atom_count,
        scientific_protocol_hash=stable_hash(ifct_spec), source_geometry_hash=file_hash(Path(source.geometry_path)),
        execution_layout={'working_directory': str(attempt), 'attempt_identifier': attempt.name},
        execution_attempt={'attempt_id': attempt.name}, settings_identity=preview_runtime)
    return ifct_spec, rendered.stdin_text, [o.to_dict() for o in rendered.outputs]


def check(spec):
    """Check one supported profile without probes, writes or registration."""
    findings = []
    domain = 'input'
    try:
        science = spec.get('scientific', {})
        profile = science.get('profile')
        if profile not in PROFILES:
            raise Unsupported('Supported Jobs profiles are FMO, ESP, IGMH and IFCT; NTO/HEA remain typed APIs')
        allowed = {'profile', 'source_result'} | ({'grid_spacing_bohr'} if profile in {'FMO', 'ESP'} else {'fragments_file', 'configuration_file'} if profile == 'IGMH' else {'orca_output', 'spin_manifold', 'state_index', 'atom_count', 'fragments'})
        if set(science) - allowed:
            raise Unsupported('Unknown scientific fields for the selected Multiwfn profile')
        dependencies = []
        domain = 'runtime'
        runtime = _runtime(spec, dependencies)
        findings.append({'domain': domain, 'status': 'WARNING' if runtime['qualification'] == 'synthetic' else 'READY',
                         'message': 'Recorded version/menu and dependency bytes checked without engine execution; synthetic evidence is not native qualification' if runtime['qualification'] == 'synthetic' else 'Recorded target-local runtime contract and dependency bytes verified without probing'})
        domain = 'source provenance'
        source_path = _absolute(science.get('source_result'), 'scientific.source_result')
        source = validate_source_result(source_path)
        source_record = read_json(source_path)
        source_attempt = source_record.get('attempt', {}).get('attempt_id')
        if not isinstance(source_attempt, str) or not source_attempt:
            raise ValueError('The authoritative source result lacks an exact source attempt identity')
        dependencies.extend(_dependency(p) for p in (source_path, Path(source.wavefunction_path), Path(source.geometry_path)))
        findings.append({'domain': domain, 'status': 'READY', 'message': 'Authoritative source result, attempt, wavefunction and ordered geometry verified'})
        domain = 'output/layout'
        workdir = _absolute(spec.get('workdir'), 'workdir')
        if workdir.exists() and (not workdir.is_dir() or any(workdir.iterdir())):
            raise ValueError('Reviewed Multiwfn workdir must be a new or empty directory; existing results are never overwritten')
        parent = workdir.parent
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise ValueError('Output storage is unavailable or not writable')
        attempt = workdir / 'scientific' / 'ifct' / 'attempt_001'
        domain = 'input/dependencies'
        if profile == 'IFCT':
            scientific_plan, menu, outputs = _ifct(science, source, runtime, attempt, dependencies)
            relative_runner = 'multiwfn/run_ifct.sh'
        else:
            fragments = _absolute(science['fragments_file'], 'scientific.fragments_file') if profile == 'IGMH' else None
            configuration = _absolute(science['configuration_file'], 'scientific.configuration_file') if profile == 'IGMH' else None
            for path in (fragments, configuration):
                if path is not None:
                    if path.stat().st_size > 1024**2:
                        raise Unsupported('Fragment/configuration metadata exceeds the 1 MiB bound')
                    dependencies.append(_dependency(path))
            scientific_plan = plan_analysis(operation=AnalysisOperation(profile), source_path=source_path,
                output_root=workdir / 'scientific', grid_spacing_bohr=science.get('grid_spacing_bohr'),
                threads=runtime['requested_nthreads'], fragment_path=fragments, igmh_config_path=configuration)
            attempt = Path(scientific_plan['target_path']).parent / 'attempts' / 'attempt_001' / 'work'
            menu = menu_stream({'FMO': Operation.FMO, 'ESP': Operation.ESP, 'IGMH': Operation.IGMH}[profile], runtime['version'], scientific_plan['configuration'])
            outputs = scientific_plan['output_contract']
            relative_runner = {'FMO': 'workflows/generate_homo_lumo_cubes.sh', 'ESP': 'workflows/generate_esp_cubes.sh', 'IGMH': 'workflows/generate_igmh_cubes.sh'}[profile]
        from cmw.jobs.runner_assets import runner_dependencies, runner_path
        dependencies.extend(runner_dependencies(relative_runner))
        prepared_path = workdir / 'prepared.json'
        runner = runner_path(relative_runner)
        plan = {'engine': 'Multiwfn', 'profile': profile, 'cwd': str(workdir),
                'argv': ['/bin/bash', str(runner), '--prepared-plan', str(prepared_path)],
                'env': {'PYTHON_BIN': sys.executable, 'MULTIWFN_EXE': runtime['executable'],
                        'MULTIWFN_NTHREADS': str(runtime['requested_nthreads'])},
                'runtime': runtime, 'scientific_plan': scientific_plan,
                'source_target_id': source.target_id, 'source_attempt_id': source_attempt,
                'source_result': str(source_path), 'source_wavefunction': source.wavefunction_path,
                'source_geometry': source.geometry_path,
                'menu_sha256': hashlib.sha256(menu.encode()).hexdigest(), 'menu_text': menu,
                'expected_outputs': outputs, 'prepared_path': str(prepared_path),
                'dependencies': list({item['path']: item for item in dependencies}.values()),
                'suspension': {'eligible': False, 'reason': 'Multiwfn profiles have no enrolled suspension contract'},
                'progress_sources': []}
        findings.extend([{'domain': 'input/dependencies', 'status': 'READY', 'message': f'{profile} versioned menu, source dependencies and exact outputs checked'},
                         {'domain': 'resources', 'status': 'WARNING', 'message': 'Explicit thread count equals the CPU commitment; memory is a declared total commitment, not a Multiwfn hard limit or prediction'},
                         {'domain': 'monitoring', 'status': 'READY', 'message': 'Preparation binds exact native log, runner events and validator record; no progress percentage is inferred'},
                         {'domain': 'execution eligibility', 'status': 'READY', 'message': 'Prepared noninteractive foreground runner; normal cancellation only'}])
        return {'status': 'READY', 'findings': findings, 'plan': plan}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        status = 'UNSUPPORTED' if isinstance(exc, Unsupported) else 'UNASSESSED' if isinstance(exc, Unassessed) else 'ERROR'
        return {'status': status, 'findings': [*findings, {'domain': domain, 'status': status, 'message': str(exc)}], 'plan': None}


def prepare(spec, checked):
    """Materialize the reviewed attempt intentionally; never launch an engine."""
    if checked.get('status') != 'READY':
        raise ValueError('Multiwfn preparation requires READY preflight')
    plan = dict(checked['plan'])
    for identity in plan['dependencies']:
        if file_hash(Path(identity['path'])) != identity['sha256']:
            raise ValueError(f'Reviewed dependency changed: {identity["path"]}')
    workdir = Path(plan['cwd'])
    if not workdir.is_dir() or any(workdir.iterdir()):
        raise ValueError('Preparation requires an exclusively created empty workdir')
    profile = plan['profile']
    if profile == 'IFCT':
        attempt = workdir / 'scientific' / 'ifct' / 'attempt_001'
        attempt.mkdir(parents=True)
        result_path = attempt / 'result.json'
    else:
        scientific = prepare_analysis(plan['scientific_plan'])
        plan['scientific_plan'] = scientific
        attempt = Path(scientific['attempt_directory'])
        result_path = Path(scientific['result_path'])
    reviewed = plan['runtime']
    settings = prepare_settings(Path(reviewed['settings_source']), attempt, reviewed['requested_nthreads'])
    runtime = MultiwfnRuntime(executable=reviewed['executable'], executable_sha256=reviewed['executable_sha256'],
        version=reviewed['version'], menu_contract=reviewed['menu_contract'], requested_nthreads=reviewed['requested_nthreads'],
        settings_mechanism='run_local_multiwfnpath', settings_source=reviewed['settings_source'],
        settings_source_sha256=reviewed['settings_source_sha256'], settings_path=str(settings),
        settings_sha256=file_hash(settings), openmp_capability='UNKNOWN', prepared_at=datetime.now(timezone.utc).isoformat()).to_dict()
    atomic_write_json(attempt / 'runtime.json', {'schema_version': 1, 'runtime': runtime})
    if profile == 'IFCT':
        from .ifct_job import prepare as prepare_ifct
        ifct_spec = workdir / 'ifct-spec.json'
        atomic_write_json(ifct_spec, plan['scientific_plan'])
        prepare_ifct(ifct_spec, attempt)
        log = attempt / next(output['output_path'] for output in plan['expected_outputs'] if output['role'] == 'session_log')
        log.parent.mkdir(parents=True, exist_ok=True)
    else:
        write_menu(target_path=Path(plan['scientific_plan']['target_path']), runtime_path=attempt / 'runtime.json', destination=attempt / 'menu.in')
        log = attempt / 'multiwfn.log'
    if file_hash(attempt / 'menu.in') != plan['menu_sha256']:
        raise ValueError('Prepared menu differs from reviewed menu')
    plan.update(attempt_directory=str(attempt), scientific_attempt_id=attempt.name if profile == 'IFCT' else plan['scientific_plan']['attempt_id'],
                result_path=str(result_path), native_log=str(log), runtime=runtime)
    plan['progress_sources'] = [{'role': 'multiwfn_session', 'path': str(log), 'stage': profile},
                                {'role': 'multiwfn_events', 'path': str(attempt / 'jobs-events.jsonl'), 'stage': profile, 'optional': True},
                                {'role': 'multiwfn_validation', 'path': str(attempt / 'jobs-validation.jsonl'), 'stage': profile, 'optional': True}]
    atomic_write_json(Path(plan['prepared_path']), plan)
    events_path = attempt / 'jobs-events.jsonl'
    with events_path.open('xb') as stream:
        info = os.fstat(stream.fileno())
    events_identity = [info.st_dev, info.st_ino]
    association = {'schema': 2, 'profile': profile,
        'stream_id': str(uuid4()), 'stream_generation': 1,
        'request_id': spec['request_id'], 'events_identity': events_identity,
        'native_path': str(log),
        'source_wavefunction_path': plan['source_wavefunction'],
        'source_geometry_path': plan['source_geometry'],
        'source_wavefunction_sha256': file_hash(Path(plan['source_wavefunction'])),
        'source_geometry_sha256': file_hash(Path(plan['source_geometry'])),
        'attempt_directory': str(attempt), 'scientific_attempt_id': plan['scientific_attempt_id'],
        'source_target_id': plan['source_target_id'], 'source_attempt_id': plan['source_attempt_id'],
        'source_result': plan['source_result'], 'prepared_path': plan['prepared_path'],
        'version': runtime['version'], 'threads': runtime['requested_nthreads'],
        'settings_sha256': runtime['settings_sha256'], 'menu_sha256': plan['menu_sha256'],
        'expected_roles': ', '.join(output['role'] for output in plan['expected_outputs'] if output.get('required', True)),
        **({'state_index': spec['scientific']['state_index'],
            'spin_manifold': spec['scientific']['spin_manifold']} if profile == 'IFCT' else {})}
    if profile in {'IGMH', 'IFCT'}:
        fragments = (spec['scientific']['fragments'] if profile == 'IFCT' else
            [{'label': label, 'indices_one_based': plan['scientific_plan']['configuration'][key]}
             for label, key in (('A', 'fragment_a'), ('B', 'fragment_b'))])
        association.update(fragment_labels=[item['label'] for item in fragments],
            fragment_counts=[len(item.get('indices_zero_based', item.get('indices_one_based', ()))) for item in fragments],
            fragment_sha256=stable_hash(fragments))
    metadata = attempt / 'jobs-association.json'
    metadata.write_text(json.dumps(association, sort_keys=True) + '\n')
    plan['progress_sources'].append({'role': 'multiwfn_metadata', 'path': str(metadata), 'stage': profile,
        'optional': True, 'expected_sha256': file_hash(metadata), 'expected_size': metadata.stat().st_size})
    # Re-write only before this prepared plan becomes a registered immutable dependency.
    atomic_write_json(Path(plan['prepared_path']), plan)
    atomic_write_json(attempt / 'jobs-events-cursor.json',
        {'sequence': 0, 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest(),
         'event_source_identity': events_identity})
    event(attempt, 'prepared')
    immutable = [Path(plan['prepared_path']), attempt / 'runtime.json', attempt / 'menu.in',
                 attempt / 'jobs-association.json', Path(runtime['settings_path'])]
    if profile == 'IFCT':
        immutable.extend([workdir / 'ifct-spec.json', attempt / 'plan.json', attempt / 'cmw-orca-excited-state.out'])
    else:
        immutable.extend([Path(plan['scientific_plan']['target_path']), attempt / 'command.json', attempt / 'analysis-plan.json'])
    plan['dependencies'] = [*plan['dependencies'], *(_dependency(path) for path in immutable)]
    return plan


def verify_prepared(path, *, terminal=False):
    plan = read_json(path)
    for identity in plan['dependencies']:
        if file_hash(Path(identity['path'])) != identity['sha256']:
            raise ValueError(f'Prepared dependency changed: {identity["path"]}')
    source = validate_source_result(Path(plan['source_result']))
    if source.target_id != plan['source_target_id']:
        raise ValueError('Source target identity changed')
    attempt = Path(plan['attempt_directory'])
    if file_hash(attempt / 'menu.in') != plan['menu_sha256']:
        raise ValueError('Prepared stdin menu changed')
    runtime = read_json(attempt / 'runtime.json')['runtime']
    if runtime != plan['runtime'] or file_hash(Path(runtime['settings_path'])) != runtime['settings_sha256']:
        raise ValueError('Prepared runtime/settings identity changed')
    if not terminal and ((attempt / 'process.exitcode').exists() or Path(plan['result_path']).exists()):
        raise ValueError('Prepared attempt has already executed; results must not be overwritten')
    return plan


def _event_json(path, *, limit=MAX_EVENT_BYTES):
    """Read only a named, bounded regular record; never follow replacement links."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Runner evidence record is not a bounded regular file')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Runner evidence record exceeds its bound')
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError('Runner evidence record must be an object')
    return result


def _validator_projection(attempt, association):
    plan = read_json(Path(association['prepared_path']))
    result_path = Path(plan['result_path'])
    result = read_json(result_path)
    valid = result.get('validation_status') == 'PASSED' if plan['profile'] == 'IFCT' else result.get('scientific', {}).get('status') == 'VALID'
    if not valid:
        raise ValueError('Existing finalizer did not record scientific validation')
    if plan['profile'] == 'IFCT':
        matches = (result.get('process_exit_code') == 0
            and result.get('plan') == str(attempt / 'plan.json'))
    else:
        matches = (result.get('execution', {}).get('process_exit_code') == 0
            and result.get('attempt', {}).get('attempt_id') == plan['scientific_attempt_id']
            and result.get('target', {}).get('target_id') == plan['scientific_plan']['target_id'])
    if not matches:
        raise ValueError('Validator result belongs to a different scientific attempt or process outcome')
    native_path = Path(association['native_path'])
    if str(native_path) != plan['native_log']:
        raise ValueError('Validator native source differs from the prepared attempt')
    native = native_path.stat()
    if not stat.S_ISREG(native.st_mode):
        raise ValueError('Validator native evidence must be a regular file')
    return {**association, 'kind': 'validator_record', 'status': 'PASSED',
            'native_path': str(native_path), 'native_identity': [native.st_dev, native.st_ino],
            'native_file_version': [native.st_size, native.st_mtime_ns, native.st_ctime_ns],
            'result_path': str(result_path), 'result_sha256': file_hash(result_path),
            'validated_roles': [o['role'] for o in plan['expected_outputs']]}


def event(attempt, kind, value=None):
    """Append one actual lifecycle boundary to the prepared, identity-pinned stream."""
    if kind not in EVENTS:
        raise ValueError('Unknown runner lifecycle event')
    association = _event_json(attempt / 'jobs-association.json')
    if association.get('schema') != 2 or association.get('attempt_directory') != str(attempt):
        raise ValueError('Runner events require this exact schema-2 prepared attempt')
    projection = None
    if kind == 'validation_passed':
        if (attempt / 'jobs-validation.jsonl').exists():
            raise ValueError('Validator evidence already exists; never overwrite it')
        # Verify the established finalizer result before publishing a success event.
        projection = _validator_projection(attempt, association)
    fd = os.open(attempt / 'jobs-events.jsonl', os.O_RDWR | os.O_APPEND | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'r+b') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(stream.fileno())
        identity = [info.st_dev, info.st_ino]
        if not stat.S_ISREG(info.st_mode) or identity != association.get('events_identity'):
            raise ValueError('Runner event source identity changed')
        if info.st_size > MAX_EVENT_BYTES:
            raise ValueError('Runner event stream exceeds its bound')
        data = stream.read(MAX_EVENT_BYTES + 1)
        cursor = _event_json(attempt / 'jobs-events-cursor.json', limit=4096)
        if (cursor.get('event_source_identity') != identity or cursor.get('size') != len(data)
                or cursor.get('sha256') != hashlib.sha256(data).hexdigest()):
            raise ValueError('Runner event stream changed or was truncated')
        records = [json.loads(line) for line in data.splitlines()]
        if cursor.get('sequence') != len(records):
            raise ValueError('Runner event sequence checkpoint differs')
        for sequence, record in enumerate(records, 1):
            if (not isinstance(record, dict) or record.get('sequence') != sequence
                    or record.get('event_source_identity') != identity
                    or any(record.get(key) != item for key, item in association.items())):
                raise ValueError('Runner event sequence or association changed')
        if kind == 'prepared' and records or kind != 'prepared' and not records:
            raise ValueError('Runner event stream must begin with one prepared event')
        if records and records[-1].get('kind') not in RUNNER_EVENT_PREDECESSORS[kind]:
            raise ValueError('Runner lifecycle boundary is out of order or already recorded')
        if kind in {'child_launched', 'process_exited'}:
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError('Runner child/process evidence requires an integer')
            parsed = int(value)
            if (kind == 'child_launched' and parsed < 1) or (kind == 'process_exited' and not 0 <= parsed <= 255):
                raise ValueError('Runner child/process evidence is outside its valid range')
        if any(record.get('kind') == kind for record in records):
            raise ValueError('Runner lifecycle boundary already recorded')
        if any(record.get('kind') in {'validation_passed', 'validation_failed'} for record in records):
            raise ValueError('Runner event stream is already terminal')
        sequence = len(records) + 1
        record = {**association, 'sequence': sequence, 'event_source_identity': identity,
                  'kind': kind, 'time': datetime.now(timezone.utc).isoformat(), 'value': value}
        encoded = (json.dumps(record, sort_keys=True) + '\n').encode()
        if len(data) + len(encoded) > MAX_EVENT_BYTES:
            raise ValueError('Runner event stream exceeds its bound')
        stream.write(encoded)
        stream.flush()
        atomic_write_json(attempt / 'jobs-events-cursor.json',
            {'sequence': sequence, 'size': len(data) + len(encoded),
             'sha256': hashlib.sha256(data + encoded).hexdigest(), 'event_source_identity': identity})
        if projection is not None:
            projection.update(sequence=sequence, event_source_identity=identity)
            with (attempt / 'jobs-validation.jsonl').open('x', encoding='utf-8') as handle:
                handle.write(json.dumps(projection, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    verify = sub.add_parser('verify-prepared'); verify.add_argument('path', type=Path)
    final = sub.add_parser('verify-finalize'); final.add_argument('path', type=Path)
    events = sub.add_parser('event'); events.add_argument('attempt', type=Path); events.add_argument('kind', choices=sorted(EVENTS)); events.add_argument('--value')
    args = parser.parse_args()
    if args.action in {'verify-prepared', 'verify-finalize'}:
        print(json.dumps(verify_prepared(args.path, terminal=args.action == 'verify-finalize'), sort_keys=True))
    else:
        event(args.attempt, args.kind, args.value)


if __name__ == '__main__':
    main()
