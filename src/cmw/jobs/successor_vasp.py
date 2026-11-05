"""Read-only VASP successor resolution and explicit four-input publication.

Geometry evidence is independent of convergence. The owner service must bind the
selected directory to the exact parent and prove its producing domain is closed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from cmw.core.preparation_publication import publish_preparation
from cmw.core.provenance import stable_hash
from cmw.periodic.vasp.inputs import INPUT_NAMES, parse_poscar
from cmw.periodic.vasp.preparation import compare_inputs, resolve_preparation
from cmw.periodic.vasp.result_finalization import verify_finalization
from cmw.periodic.vasp.results import inspect_result
from .review_engines import _Sources
from .successor_models import SuccessorUnverifiable

MAX_SOURCE_BYTES = 64 * 1024 * 1024
_OPTIONS = {'directory', 'preparation_spec', 'output', 'scratch_root', 'record_directory',
            'scratch_mount', 'segment', 'stdout', 'accepted_record', 'accepted_artifact_id',
            'source_mode', 'allow_structure_changes'}
_REQUIRED = {'directory', 'preparation_spec', 'output', 'scratch_root', 'record_directory', 'source_mode'}


class VaspSuccessorError(ValueError):
    code = 'VASP_SUCCESSOR_REFUSED'


def _require(condition, message):
    if not condition:
        raise VaspSuccessorError(message)


def _path(value, base=None):
    _require(isinstance(value, (str, Path)) and bool(str(value).strip()), 'Source path is required')
    path = Path(value).expanduser()
    path = path if path.is_absolute() else (base or Path.cwd()) / path
    _require(path == path.resolve(), f'Original canonical source path required: {path}')
    return path


def _read(sources, path, role):
    record = sources.observe(str(path), role)
    with path.open('rb') as stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)
    _require(len(data) <= MAX_SOURCE_BYTES and hashlib.sha256(data).hexdigest() == record['sha256'],
             f'Source changed while reading: {path}')
    return data


def _source_identity(record):
    return {key: value for key, value in record.items() if key != 'observed_at'}


def _bound_preparation_sources(path, sources):
    """Bound the existing resolver's explicit source closure before it reads it."""
    data = sources.json(str(path), 'successor_specification')
    _require(isinstance(data, dict), 'Preparation specification must be an object')
    base = path.parent
    profile = data.get('profile')
    profile_base = base
    if isinstance(profile, str):
        profile_path = _path(profile, base)
        profile = sources.json(str(profile_path), 'successor_profile')
        profile_base = profile_path.parent
    _require(isinstance(profile, dict), 'Preparation profile is required')
    structure = _path(data.get('structure'), base)
    _read(sources, structure, 'successor_structure')
    overrides = data.get('overrides', {})
    _require(isinstance(overrides, dict), 'Preparation overrides must be an object')
    potentials = profile.get('potentials', {})
    _require(isinstance(potentials, dict), 'Preparation potentials must be an object')
    override_potentials = overrides.get('potentials', {})
    _require(isinstance(override_potentials, dict), 'Potential overrides must be an object')
    potentials = {**potentials, **override_potentials}
    root = _path(potentials.get('root'), base if 'root' in override_potentials else profile_base)
    variants = potentials.get('variants')
    _require(isinstance(variants, dict) and 0 < len(variants) <= 118, 'Explicit bounded potential variants required')
    for variant in variants.values():
        _require(isinstance(variant, str) and Path(variant).name == variant and variant not in ('.', '..'),
                 'Potential variant must be one library directory name')
        sources.observe(str(_path(root / variant / 'POTCAR')), 'selected_potential')
    if data.get('source_record'):
        sources.json(str(_path(data['source_record'], base)), 'structure_preparation_record')
    if 'baseline' in data:
        baseline = data['baseline']
        if isinstance(baseline, dict):
            record_path = _path(baseline.get('record'), base)
            record = sources.json(str(record_path), 'baseline_record')
            baseline = record.get('publication', {}).get('input_path')
            baseline = _path(baseline, record_path.parent)
        else:
            baseline = _path(baseline, base)
        for name in INPUT_NAMES:
            candidate = baseline / name
            if candidate.exists():
                sources.observe(str(candidate), 'baseline_input')
    return structure


def _observed(directory, options, sources):
    for name in ('OUTCAR', 'OSZICAR', 'POSCAR', 'CONTCAR', 'INCAR'):
        path = directory / name
        if path.exists():
            sources.observe(str(path), name)
    if options.get('stdout'):
        sources.observe(str(_path(options['stdout'])), 'stdout')
    evidence = inspect_result(directory, segment=options.get('segment'), stdout=options.get('stdout'),
                              max_bytes=MAX_SOURCE_BYTES)
    _require(evidence.get('status') == 'inspected' and evidence.get('mode', {}).get('supported'),
             'Parent requires supported static/fixed-cell native VASP evidence')
    _require(not evidence.get('conflicts'), 'Native source correspondence contains conflicts')
    _require(evidence['coverage']['all_sources_complete'] and evidence['coverage']['all_sources_stable'],
             'Parent sources are incomplete or changed during observation')
    endpoint = evidence.get('endpoint', {})
    current = evidence.get('selected_segment', {})
    _require(endpoint.get('geometry') and endpoint.get('is_last_complete_evaluation')
             and endpoint.get('is_final_segment') and not current.get('trailing_incomplete'),
             'Last native evaluation has no complete evaluated geometry')
    comparisons = endpoint['comparisons']
    _require(comparisons['initial_poscar']['status'] == 'match'
             and comparisons['fixed_cell']['status'] == 'match',
             'Native initial atom order/cell cannot be matched to the parent POSCAR')
    relaxation = evidence['mode']['calculation'] == 'fixed-cell-relaxation'
    role = 'CONTCAR' if relaxation else 'POSCAR'
    _require(comparisons['endpoint_contcar' if relaxation else 'endpoint_poscar']['status'] == 'match',
             f'{role} is partial, unrelated, or is not the last evaluated native geometry')
    if relaxation:
        _require(evidence['constraints']['contcar_flags_match'] is True,
                 'Evaluated geometry constraints cannot be matched to the parent POSCAR')
    evaluation = current['evaluations'][endpoint['evaluation_index']]
    return directory / role, {
        'qualification': 'observed-complete', 'scientific_acceptance': 'not established',
        'source_role': role, 'snapshot_id': evidence['snapshot_id'],
        'segment_id': current['segment_id'], 'evaluation_index': endpoint['evaluation_index'],
        'native_evaluation': evaluation['native_ionic_label'],
        'electronic_native_converged': evaluation.get('native_convergence'),
        'electronic_numeric_convergence': evaluation.get('numeric_convergence'),
        'ionic_native_stop_observed': bool(evidence['ionic'].get('native_stopping_reference')),
        'comparisons': comparisons, 'geometry': endpoint['geometry'],
        'constraints_qualification': 'Current POSCAR flags corroborated by native initial geometry and endpoint flags; not a pre-run receipt',
    }


def _accepted(options, parent, state_directory, observed, sources):
    path = _path(options.get('accepted_record'))
    record = sources.json(str(path), 'accepted_record')
    _require(isinstance(record, dict), 'Accepted finalization record must be an object')
    required = record.get('required_sources')
    _require(isinstance(required, list) and 0 < len(required) <= 128,
             'Accepted record source closure must contain 1..128 sources')
    for source in required:
        _require(isinstance(source, dict) and isinstance(source.get('resolved_path'), str),
                 'Accepted record has malformed source identity')
        sources.observe(str(_path(source['resolved_path'])), 'accepted_required_source')
    checked = verify_finalization(path)
    if checked['valid'] is None:
        raise SuccessorUnverifiable('Accepted finalization is unverifiable; retry: ' + '; '.join(checked['findings']))
    if checked['valid'] is not True:
        raise VaspSuccessorError('Accepted finalization no longer verifies: ' + '; '.join(checked['findings']))
    evidence = checked['record']['evidence']
    execution = evidence['binding']['execution']
    job = execution['job']
    _require(job['id'] == parent['job_id'] and job['attempt_id'] == parent['attempt_id']
             and execution['binding'].get('state_directory') == str(_path(state_directory)),
             'Accepted record belongs to a different parent attempt or original Jobs state')
    identity = execution['binding'].get('queue_identity')
    if identity is not None:
        _require(all(identity.get(key) == parent[key] for key in ('node_id', 'queue_id')),
                 'Accepted record belongs to a different owner node/queue')
    _require(evidence['snapshot_id'] == observed['snapshot_id'],
             'Accepted record does not describe the selected native source snapshot')
    matches = [artifact for artifact in checked['artifacts']
               if artifact.artifact_id == options.get('accepted_artifact_id')]
    _require(len(matches) == 1, 'Explicit accepted artifact ID is missing from the verified record')
    artifact = matches[0]
    _require(artifact.metadata.get('periodic_geometry') == observed['geometry'],
             'Selected accepted artifact is not the evaluated periodic structure')
    return {**observed, 'qualification': 'accepted-periodic-structure',
            'scientific_acceptance': 'PASS under the recorded explicit policy only',
            'artifact_id': artifact.artifact_id, 'record_id': checked['record']['record_id']}


def _classify(change):
    category, field = change['category'], change.get('field')
    if category in ('coordinates', 'cell'):
        return 'geometry/cell'
    if category == 'species_blocks':
        return 'atom/species order'
    if category == 'constraints':
        return 'constraints'
    if category == 'kpoint_sampling':
        return 'k sampling'
    if category == 'potential_content':
        return 'potentials'
    if category == 'runtime_parallelization':
        return 'resources'
    if category == 'scientific_incar':
        if field in ('ISTART', 'ICHARG', 'LWAVE', 'LCHARG'):
            return 'checkpoint semantics'
        if field in {'EDIFF', 'EDIFFG', 'NELM', 'NELMIN', 'NELMDL', 'NSW', 'IBRION', 'POTIM',
                     'ALGO', 'IALGO', 'AMIX', 'BMIX', 'AMIX_MAG', 'BMIX_MAG', 'IMIX', 'INIMIX', 'MAXMIX'}:
            return 'solver/convergence controls'
        return 'Hamiltonian/spin/charge'
    return category


def _resolve(options, parent_target, state_directory):
    _require(isinstance(options, dict) and not set(options) - _OPTIONS and _REQUIRED <= set(options),
             'Unsupported or missing VASP successor options')
    mode = options['source_mode']
    _require(mode in ('input', 'observed', 'accepted'), 'Source mode must be input, observed or accepted')
    allowed = options.get('allow_structure_changes', [])
    _require(isinstance(allowed, list) and len(allowed) == len(set(allowed))
             and not set(allowed) - {'geometry', 'cell', 'constraints'},
             'Structure allowances support geometry, cell and constraints only')
    _require(mode == 'accepted' or not any(key in options for key in ('accepted_record', 'accepted_artifact_id')),
             'Accepted artifacts require source_mode=accepted')
    sources = _Sources(MAX_SOURCE_BYTES, None)
    directory = _path(options['directory'])
    baseline = {name: _read(sources, directory / name, 'parent_input:' + name) for name in INPUT_NAMES}
    if mode == 'input':
        source_path = directory / 'POSCAR'
        qualification = {'qualification': 'reviewed-parent-input', 'source_role': 'POSCAR',
                         'scientific_acceptance': 'not asserted', 'evaluation_index': None,
                         'constraints_qualification': 'Explicit original input bytes; execution identity is a separate owner check'}
    else:
        source_path, qualification = _observed(directory, options, sources)
        if mode == 'accepted':
            qualification = _accepted(options, parent_target, state_directory, qualification, sources)
    source_bytes = _read(sources, source_path, 'selected_geometry')
    selected = parse_poscar(source_bytes)
    _require(selected['status'] == 'valid', 'Selected source geometry is not a supported complete POSCAR')
    spec_path = _path(options['preparation_spec'])
    structure_path = _bound_preparation_sources(spec_path, sources)
    files, record, publication = resolve_preparation(spec_path, output=options['output'],
        scratch_root=options['scratch_root'], record_directory=options['record_directory'],
        scratch_mount=options.get('scratch_mount'))
    for key in ('input_path', 'record_directory'):
        destination = Path(publication[key])
        _require(destination != directory and not destination.is_relative_to(directory),
                 'Successor publication must be outside the parent output directory')
    # The caller's structure may deliberately differ; no implicit rewrite is made.
    structural = compare_inputs({**files, 'POSCAR': source_bytes}, files,
                                baseline_runtime=record['expected_runtime'], prepared_runtime=record['expected_runtime'])
    _require(structural['prepared_input_comparison_complete'], 'Selected and successor structures cannot be compared')
    mapping = {'coordinates': 'geometry', 'cell': 'cell', 'constraints': 'constraints'}
    for change in structural['changes']:
        category = change['category']
        _require(category != 'species_blocks', 'Atom/species order changes need an unsupported explicit mapping contract')
        if category in mapping:
            _require(mapping[category] in allowed,
                     f'Caller must explicitly allow successor {mapping[category]} changes')
    comparison = compare_inputs(baseline, files, prepared_runtime=record['expected_runtime'])
    _require(comparison['prepared_input_comparison_complete'],
             'Parent four-input syntax/settings cannot be compared completely')
    differences = [{**change, 'successor_category': _classify(change)} for change in comparison['changes']]
    preparation_sources = {item['resolved_location']: item['sha256'] for item in record['sources']}
    for path, digest in preparation_sources.items():
        observed = sources.observe(path, 'preparation_source')
        _require(digest == observed['sha256'], 'Preparation source changed while resolving candidates')
    sources.verify_stable()
    identities = [_source_identity(source) for source in sorted(sources.records.values(), key=lambda item: item['path'])]
    preview = {'engine': 'VASP', 'parent': dict(parent_target), 'directory': str(directory),
               'source_mode': mode, 'source': {**qualification, 'path': str(source_path),
                                             'sha256': hashlib.sha256(source_bytes).hexdigest()},
               'successor_structure': str(structure_path), 'sources': identities,
               'comparison': comparison, 'differences': differences,
               'structure_changes': structural['changes'],
               'checkpoint': {'status': 'refused', 'reason': 'Only geometry/input reuse with ISTART=0, ICHARG=2; no electronic-checkpoint validity contract',
                              'copied_files': list(INPUT_NAMES)},
               'preparation': record, 'publication_plan': publication,
               'resolution_digest': stable_hash({'record': record, 'plan': publication}),
               'limitations': ['Current input evidence is not an authenticated historical execution receipt',
                               'Baseline runtime overlay is unobserved; no effective-execution equivalence claimed',
                               'Sequential source checks cannot prevent arbitrary external edits after final validation']}
    preview['digest'] = stable_hash(preview)
    return preview, files, record, publication


def preview_vasp(options, parent_target, *, state_directory):
    """Inspect explicit sources and resolve a new bundle without any writes."""
    return _resolve(options, parent_target, state_directory)[0]


def _verify_sources(current):
    fresh = _Sources(MAX_SOURCE_BYTES, None)
    for expected in current['sources']:
        actual = fresh.observe(expected['path'], expected['role'])
        _require(all(actual[key] == expected[key] for key in ('sha256', 'bytes_read', 'stat_before', 'resolved_path')),
                 'VASP source identity/bytes changed before publication')
    fresh.verify_stable()



def verify_published_parent(options, parent_target, state_directory, preview, *, recheck_sources):
    """Verify the approved source/ancestor evidence without planning a new destination."""
    if recheck_sources:
        _verify_sources(preview)
    if options['source_mode'] == 'accepted':
        sources = _Sources(MAX_SOURCE_BYTES, None)
        _accepted(options, parent_target, state_directory, preview['source'], sources)
        sources.verify_stable()


def materialize_vasp(options, parent_target, approved_preview, *, state_directory):
    """Revalidate exact approval, then publish only the resolved four input bytes."""
    current, files, record, publication = _resolve(options, parent_target, state_directory)
    _require(current == approved_preview, 'VASP sources, settings, identity or destination changed since approval')
    _verify_sources(current)
    record = {**record, 'successor': {'schema_version': 1, 'parent': dict(parent_target),
              'source': current['source'], 'plan_digest': current['digest'],
              'relationship': 'new-input-guess', 'checkpoint_reuse': False}}
    return publish_preparation(files, record, publication)
