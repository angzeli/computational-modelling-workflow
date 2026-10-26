"""Resolve reviewed molecular inputs on their owning node, without path adoption."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from cmw.jobs.locations import _read_job, discover, read_location_metadata
from cmw.jobs.store import Store
from .protocol import NodeError, target_identity
from .storage import check_storage

ENGINES = frozenset({'ORCA', 'Multiwfn'})
PATH_FIELDS = {
    'ORCA': frozenset({'input', 'structure', 'config'}),
    'Multiwfn': frozenset({'source_result', 'fragments_file', 'configuration_file', 'orca_output'}),
}
RUNTIME_PATH_FIELDS = {
    'ORCA': frozenset({'executable', 'contract'}),
    'Multiwfn': frozenset({'executable', 'settings_source', 'contract'}),
}
MAX_ARTIFACT_BYTES = 8 * 1024**3


def _require(condition, message, code='INVALID_MOLECULAR_SUBMISSION'):
    if not condition:
        raise NodeError(code, message)


def normalize_artifact(value, *, scientific_result=False):
    required = {'target', 'location_id', 'relative', 'role', 'node_path', 'sha256'}
    scientific = {'scientific_target_id', 'scientific_attempt_id'}
    _require(isinstance(value, dict) and required <= set(value)
             and not set(value) - required - scientific, 'Artifact reference fields differ from the supported contract')
    target_identity(value['target'])
    for key in ('location_id', 'sha256'):
        _require(isinstance(value[key], str) and re.fullmatch(r'[a-f0-9]{64}', value[key]), f'Artifact {key} requires a SHA-256 identity')
    from .fetch import _relative
    _relative(value['relative'], empty=True)
    _require(isinstance(value['role'], str) and 0 < len(value['role']) <= 256, 'Artifact role must match a declared Files location')
    path = value['node_path']
    _require(isinstance(path, str) and Path(path).is_absolute() and len(path) <= 4096
             and all(ord(c) >= 32 for c in path), 'Artifact node_path must disclose its exact original node-local path')
    if scientific_result or scientific & set(value):
        _require(scientific <= set(value), 'Scientific result references require target and attempt identities')
        _require(isinstance(value['scientific_target_id'], str) and re.fullmatch(r'[a-f0-9]{64}', value['scientific_target_id']),
                 'Scientific target identity must be a SHA-256 digest')
        _require(isinstance(value['scientific_attempt_id'], str) and 0 < len(value['scientific_attempt_id']) <= 256,
                 'Scientific attempt identity must be explicit')
    return deepcopy(value)


def normalize_scientific(engine, value):
    _require(engine in ENGINES and isinstance(value, dict), 'A supported molecular engine and scientific specification are required')
    _require(isinstance(value.get('profile'), str), 'Select an explicit molecular scientific profile')
    result = deepcopy(value)
    from .submission import _relative
    for key in PATH_FIELDS[engine] & value.keys():
        ref = value[key]
        _require(isinstance(ref, dict) and len(ref) == 1 and next(iter(ref)) in {'bundle', 'artifact'},
                 f'scientific.{key} requires a selected bundle or owning-node artifact reference')
        if 'bundle' in ref:
            _relative(ref['bundle'])
            _require(not (engine == 'Multiwfn' and key in {'source_result', 'orca_output'}),
                     'Multiwfn donors require authoritative target-local artifact references; copied manifests cannot adopt provenance')
        else:
            normalize_artifact(ref['artifact'], scientific_result=key == 'source_result')
    return result


def _digest_file(path):
    """Reuse the shared streamed identity without returning scientific file bytes."""
    from cmw.jobs.runner_assets import file_identity
    from .transfer import _direct
    path = _direct(path)
    return file_identity(path, max_bytes=MAX_ARTIFACT_BYTES)['sha256']


def _orca_result_authority(job):
    """A generic cwd is never an original-result declaration."""
    _require(job.get('engine') == 'ORCA', 'Scientific donors require an explicitly recorded ORCA result; a generic Command cwd is not source authority')
    bound = job.get('molecular_submission')
    if bound is not None:
        from cmw.jobs.molecular import validate_binding
        bound = validate_binding(bound)
        plan = bound['plan']
        association = plan.get('scientific_association', {})
        _require(isinstance(association.get('target_id'), str) and isinstance(plan.get('result_path'), str),
                 'Select a directly bound ORCA stage result; a compound summary is not a wavefunction source')
        return Path(plan['result_path']), association['target_id']
    from cmw.jobs.location_resolvers import _script_argv, _options, _ORCA_PAIRS
    from cmw.jobs.runner_assets import runner_path
    argv = _script_argv(job)
    _require(bool(argv) and Path(argv[0]).resolve() == runner_path('orca/run_orca.sh').resolve(),
             'Legacy donor lacks the recognized installed ORCA runner contract')
    options = _options(argv, _ORCA_PAIRS, {'--require-runtime-contract', '--replace-stale-lock'}, {'--artifact', '--execution-input'})
    _require(isinstance(options, dict) and {'--input', '--output', '--stderr', '--metadata', '--target'} <= options.keys(), 'ORCA donor lacks the complete declared stage runner contract')
    target_path = Path(options['--target'])
    result_path = Path(options['--metadata'])
    _require(target_path.is_absolute() and result_path.is_absolute(), 'ORCA donor declarations must be absolute')
    target_record = read_location_metadata(target_path)
    target = target_record.get('target', {})
    _require(isinstance(target.get('target_id'), str), 'ORCA donor target identity is unavailable')
    return result_path, target['target_id']


def _scientific_authority(job, path, record):
    original, target_id = _orca_result_authority(job)
    _require(original.resolve(strict=True) == original, 'Original ORCA result path changed or follows a symlink')
    original_record = read_location_metadata(original)
    _require(original_record.get('target', {}).get('target_id') == target_id,
             'Original ORCA result differs from its recorded scientific target')
    if path == original:
        return
    provenance = record.get('provenance', {})
    _require(provenance.get('operation') == 'orca_gbw_to_molden'
             and provenance.get('source_result') == str(original)
             and provenance.get('source_target_id') == target_id
             and provenance.get('source_attempt_id') == original_record.get('attempt', {}).get('attempt_id'),
             'Selected result is neither the declared original nor its explicit supported conversion lineage')
    from cmw.core.execution_layout import resolve_recorded_layout
    from cmw.core.job import JobTarget
    from cmw.molecular.orca.conversion import _source_contract, _result_reuse
    original_record, _, wavefunction, _, output = _source_contract(original)
    layout = resolve_recorded_layout(record['execution_layout'], metadata_path=path)
    _require(layout.target_identifier == record['target']['target_id']
             and layout.attempt_identifier == record['attempt']['attempt_id'], 'Conversion layout identity differs')
    plan_path = layout.metadata_path('conversion-plan.json')
    _require(_digest_file(plan_path) == provenance.get('conversion_plan_sha256'), 'Conversion plan identity changed')
    plan = read_location_metadata(plan_path)
    _require(plan.get('source_result') == str(original) and plan.get('result_path') == str(path)
             and plan.get('attempt_id') == record['attempt']['attempt_id'], 'Conversion plan does not bind the original result and selected attempt')
    raw = record['target']
    target = JobTarget(raw['stage_type'], raw['geometry_sha256'], raw['charge'], raw['multiplicity'], raw['calculation'])
    reusable, reason = _result_reuse(path, target=target, source_result_sha256=_digest_file(original),
        source_output_sha256=_digest_file(output), source_wavefunction_sha256=_digest_file(wavefunction),
        converter_sha256=plan['converter_sha256'])
    _require(reusable, 'Existing conversion provenance validation failed: ' + reason)


def resolve_artifact(config, value, *, scientific_result=False, verify_bytes=True):
    ref = normalize_artifact(value, scientific_result=scientific_result)
    target = ref['target']
    _require(target['node_id'] == config['node_id'], 'Donor belongs to a different node; cross-host result adoption is unsupported', 'WRONG_NODE')
    queues = [record for record in config['queues'].values() if record['queue_id'] == target['queue_id']]
    _require(len(queues) == 1, 'Donor queue is not enrolled on this execution node', 'WRONG_TARGET')
    store = Store(queues[0]['state'])
    identity = store.queue_identity()
    _require(identity is not None and identity['node_id'] == target['node_id'] and identity['queue_id'] == target['queue_id'],
             'Donor queue identity changed', 'WRONG_TARGET')
    job, _ = _read_job(store, target['job_id'])
    _require(job['attempt_id'] == target['attempt_id'], 'Donor immutable Jobs attempt changed', 'ATTEMPT_CHANGED')
    _require(job['status'] in {'Done', 'Fail', 'Cancelled'}, 'Donor must be a terminal Jobs attempt; process state is not scientific validation', 'SOURCE_NOT_TERMINAL')
    locations = discover(store, job['id'], expected_attempt_id=target['attempt_id'])
    location = next((item for item in locations.locations if item.key == ref['location_id']), None)
    _require(location is not None and not location.ambiguity and location.accessibility == 'AVAILABLE'
             and location.role == ref['role'] and location.path == location.declared_path,
             'Donor location is unavailable, changed, ambiguous or has a different role', 'LOCATION_UNAVAILABLE')
    from .transfer import _direct
    base = _direct(location.path, directory=location.kind == 'directory')
    if location.kind == 'file':
        _require(ref['relative'] == '', 'A selected file cannot carry a child path')
        path = base
    else:
        _require(ref['relative'] != '', 'Select one explicit file below the declared donor directory')
        path = _direct(base / ref['relative'])
        _require(path.is_relative_to(base), 'Donor escaped the selected location')
    _require(str(path) == ref['node_path'], 'Disclosed donor path differs from its owning-node location', 'SOURCE_CHANGED')
    _require(not path.is_relative_to(store.root) and not any(path.is_relative_to(Path(q['state'])) for q in config['queues'].values()),
             'Private Jobs state cannot be a scientific donor')
    storages = [record for record in config['storages'].values() if path.is_relative_to(Path(record['root']))]
    _require(bool(storages) and all(check_storage(record)['ready'] for record in storages), 'Donor storage is not enrolled or available', 'NOT_READY')
    if verify_bytes:
        _require(_digest_file(path) == ref['sha256'], 'Donor bytes differ from the reviewed digest', 'SOURCE_CHANGED')
    if scientific_result:
        record = read_location_metadata(path)
        _require(record.get('target', {}).get('target_id') == ref['scientific_target_id']
                 and record.get('attempt', {}).get('attempt_id') == ref['scientific_attempt_id'],
                 'Scientific result/attempt differs from the reviewed donor association', 'SOURCE_CHANGED')
        _scientific_authority(job, path, record)
        if verify_bytes:
            from cmw.molecular.workflows.source import validate_source_result
            validate_source_result(path)
    return str(path)


def resolved_spec(config, queue, spec, bundle, work, *, verify_bytes=True):
    """Only enrolled runtime paths and selected files acquire node-local authority."""
    profile = config['profiles'][spec['profile']]
    _require(profile['engine'] == spec['engine'] and isinstance(profile.get('runtime'), dict), 'Molecular profile lacks its node-local runtime contract')
    scientific = normalize_scientific(spec['engine'], spec['scientific'])
    selected = {item['path'] for item in spec['manifest']['files']}
    for key in PATH_FIELDS[spec['engine']] & scientific.keys():
        ref = scientific[key]
        if 'bundle' in ref:
            _require(ref['bundle'] in selected, f'scientific.{key} is not in the reviewed input closure')
            scientific[key] = str(bundle / ref['bundle'])
        else:
            scientific[key] = resolve_artifact(config, ref['artifact'], scientific_result=key == 'source_result', verify_bytes=verify_bytes)
    return {'schema': 1, 'request_id': spec['request_id'], 'engine': spec['engine'], 'name': spec['name'],
            'intent': 'held', 'resources': spec['resources'], 'workdir': str(work),
            'runtime': deepcopy(profile['runtime']), 'scientific': scientific}


def check(config, queue, spec, bundle, work, *, published=False):
    """Pretransfer inspection is truthful about unavailable destination input bytes."""
    resolved = resolved_spec(config, queue, spec, bundle, work)
    portable = any('bundle' in value for key, value in spec['scientific'].items() if key in PATH_FIELDS[spec['engine']])
    if published or not portable:
        from cmw.jobs.molecular import check as check_molecular
        return check_molecular(resolved)
    findings = [
        {'domain': 'runtime', 'status': 'WARNING', 'message': 'Enrolled target-local runtime files are pinned; engine-specific runtime compatibility is rechecked before registration.'},
        {'domain': 'source_provenance', 'status': 'UNASSESSED',
         'message': 'Portable files are not present on the destination before explicit transfer.'},
        {'domain': 'input', 'status': 'UNASSESSED', 'message': 'Full shared molecular preflight runs on published destination bytes before any held registration.'},
        {'domain': 'execution', 'status': 'UNASSESSED', 'message': 'Preview/check authorizes no execution and does not establish scientific validity.'},
    ]
    return {'status': 'UNASSESSED', 'findings': findings, 'plan': None, 'resolved_spec': resolved}
