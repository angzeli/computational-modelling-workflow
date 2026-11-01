"""Bounded caller-defined VASP studies using the existing standalone preparer.

The graph describes independent scientific cases. VASP has no generic execution
renderer here: input publication stays with its four-file split publisher and
held registration stays with Jobs. No function in this module launches a job.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json
import math
from pathlib import Path
import re

from cmw.core.preparation_publication import content_identity, publication_plan, publish_preparation
from cmw.core.provenance import stable_hash
from cmw.core.workflow_graph import CalculationNode, WorkflowGraph
from cmw.periodic.vasp.inputs import INPUT_NAMES, check_input_bytes
from cmw.periodic.vasp.preparation import (
    PreparationSpec, compare_inputs, load_spec, resolve_preparation_spec,
)

MAX_CASES = 64
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
VARIED_FIELDS = frozenset({'ENCUT', 'k_sampling', 'geometry', 'cell', 'composition',
                          'species_order', 'constraints'})
_FIELDS = {'schema', 'study_id', 'name', 'engine', 'baseline', 'preparation_spec', 'matrix',
           'cases', 'varied', 'scientific_policy', 'metric', 'normalization', 'comparison_rule',
           'execution', 'output_root', 'scratch_root', 'record_root', 'scratch_mount'}
_REQUIRED = _FIELDS - {'cases', 'scratch_mount'}


class StudyPlanError(ValueError):
    code = 'STUDY_PLAN_REFUSED'


def _require(condition, message):
    if not condition:
        raise StudyPlanError(message)


def _object(value, fields, required, label):
    _require(isinstance(value, dict) and not set(value) - fields and required <= set(value),
             f'Invalid or incomplete {label}')
    return value


def _name(value, label):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value)
             and '..' not in value, f'{label} must be a simple 1..96 character name')
    return value


def _path(value, base=None, *, exists=True):
    _require(isinstance(value, (str, Path)) and bool(str(value).strip()), 'Explicit path required')
    path = Path(value).expanduser()
    if not path.is_absolute():
        _require(base is not None, f'Absolute path required: {path}')
        path = base / path
    _require(path == path.resolve(), f'Original canonical path required: {path}')
    if exists:
        _require(path.exists() and not path.is_symlink(), f'Missing or linked source: {path}')
    return path


def _read(path, sources, role, *, limit=MAX_SOURCE_BYTES):
    path = _path(path)
    _require(path.is_file(), f'Regular source file required: {path}')
    before = path.stat()
    _require(before.st_size <= limit, f'Source exceeds bounded {limit}-byte reader: {path}')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    after = path.stat()
    _require(len(data) <= limit and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
             f'Source changed during observation: {path}')
    identity = {'path': str(path), **content_identity(data)}
    old = sources.get(str(path))
    _require(old is None or all(old[key] == identity[key] for key in identity),
             f'Source changed during plan: {path}')
    sources[str(path)] = {**identity, 'roles': sorted(set((old or {}).get('roles', [])) | {role})}
    return data


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, f'Duplicate JSON field: {key}')
            result[key] = value
        return result
    def nonfinite(value):
        raise StudyPlanError(f'Nonfinite JSON value: {value}')
    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)
    except RecursionError as exc:
        raise StudyPlanError('Study JSON nesting exceeds the supported parser depth') from exc


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _identity(value, data, label):
    _object(value, {'sha256', 'size_bytes'}, {'sha256', 'size_bytes'}, label)
    _require(re.fullmatch('[0-9a-f]{64}', str(value['sha256'])) is not None
             and type(value['size_bytes']) is int and value['size_bytes'] >= 0,
             f'{label} requires a reviewed SHA-256 and byte count')
    _require(value == content_identity(data), f'{label} differs from the reviewed identity; baseline is never refreshed automatically')


def _matrix(manifest):
    matrix = _object(manifest['matrix'], {'encut', 'kmeshes', 'structures'},
                     {'encut', 'kmeshes', 'structures'}, 'matrix')
    for key in matrix:
        _require(isinstance(matrix[key], list) and 0 < len(matrix[key]) <= MAX_CASES,
                 f'matrix.{key} must contain 1..{MAX_CASES} explicit values')
    explicit = manifest.get('cases')
    if explicit is None:
        count = math.prod(len(matrix[key]) for key in ('encut', 'kmeshes', 'structures'))
    else:
        _require(isinstance(explicit, list), 'cases must be an explicit list')
        count = len(explicit)
    # Bound the declared expansion before producing combinations or reading structures.
    _require(0 < count <= MAX_CASES, f'Study has {count} cases; maximum is {MAX_CASES}')
    energies = matrix['encut']
    _require(all(_positive(item) for item in energies) and len(set(energies)) == len(energies),
             'ENCUT values must be distinct finite positive numbers')
    meshes = {}
    for item in matrix['kmeshes']:
        _object(item, {'name', 'mode', 'mesh', 'shift'}, {'name', 'mode', 'mesh', 'shift'}, 'k mesh')
        name = _name(item['name'], 'k-mesh name')
        _require(name not in meshes, 'Duplicate k-mesh name')
        _require(item['mode'] in ('Gamma', 'Monkhorst-Pack')
                 and isinstance(item['mesh'], list) and len(item['mesh']) == 3
                 and all(type(n) is int and n > 0 for n in item['mesh'])
                 and isinstance(item['shift'], list) and len(item['shift']) == 3
                 and all(type(n) in (int, float) and math.isfinite(n) for n in item['shift']),
                 'Explicit regular mesh mode, three positive mesh integers and three finite shifts required')
        meshes[name] = item
    structures = {}
    for item in matrix['structures']:
        _object(item, {'name', 'path', 'sha256'}, {'name', 'path', 'sha256'}, 'reviewed structure')
        name = _name(item['name'], 'structure name')
        _require(name not in structures and re.fullmatch('[0-9a-f]{64}', str(item['sha256'])) is not None,
                 'Each structure requires a unique name and reviewed SHA-256')
        structures[name] = item
    if explicit is None:
        cases = []
        for structure, encut, mesh in itertools.product(structures, energies, meshes):
            # Ordinals preserve caller ordering and avoid rounding collisions in names.
            cases.append({'name': f'case-{len(cases)+1:03d}-{structure}-{mesh}',
                          'structure': structure, 'encut': encut, 'kmesh': mesh})
    else:
        cases = explicit
    names = set()
    for case in cases:
        _object(case, {'name', 'structure', 'encut', 'kmesh'},
                 {'name', 'structure', 'encut', 'kmesh'}, 'case')
        name = _name(case['name'], 'case name')
        _require(name not in names, 'Case names must be unique even for identical content')
        names.add(name)
        _require(isinstance(case['structure'], str) and isinstance(case['kmesh'], str)
                 and case['structure'] in structures and case['kmesh'] in meshes
                 and type(case['encut']) in (int, float) and case['encut'] in energies,
                 'Every named case must use declared matrix values')
    return cases, structures, meshes


def _field(change):
    category, key = change['category'], change.get('field')
    if category == 'scientific_incar':
        if key == 'ENCUT':
            return 'ENCUT'
        if key == 'NELECT':
            return 'charge'
        if key in {'ISPIN', 'MAGMOM', 'NUPDOWN', 'LNONCOLLINEAR', 'LSORBIT'}:
            return 'spin'
        return 'fixed_incar'
    return {'coordinates': 'geometry', 'cell': 'cell', 'constraints': 'constraints',
            'species_blocks': 'composition' if key == 'counts' else 'species_order',
            'kpoint_sampling': 'k_sampling', 'potential_content': 'potentials',
            'runtime_parallelization': 'resources'}.get(category, category)


def _semantic(files):
    checked = check_input_bytes(files)
    inputs = checked['inputs']
    structure, incar, mesh = inputs['POSCAR'], inputs['INCAR'], inputs['KPOINTS']
    settings = incar['settings']
    return {'status': 'assessed' if checked['status'] == 'valid' and not incar.get('unknown_tags') else 'unassessed',
            'geometry': structure.get('cartesian_coordinates'), 'cell': structure.get('cell'),
            'species_order': structure.get('species'), 'composition': structure.get('counts'),
            'atom_count': structure.get('atom_count'), 'constraints': structure.get('selective_dynamics'),
            'charge': {'NELECT': settings.get('NELECT'), 'interpretation': 'explicit electron count or native default; not independently measured charge'},
            'spin': {key: value for key, value in settings.items() if key in {'ISPIN', 'MAGMOM', 'NUPDOWN', 'LNONCOLLINEAR', 'LSORBIT'}},
            'incar': settings, 'potential_bytes': content_identity(files['POTCAR']),
            'k_sampling': {key: mesh.get(key) for key in ('mode', 'mesh', 'shift')},
            'unknown_tags': incar.get('unknown_tags', [])}


def _bound_preparation(path, sources):
    data = _json(_read(path, sources, 'preparation_specification', limit=MAX_MANIFEST_BYTES))
    _require(isinstance(data, dict), 'Preparation specification must be an object')
    profile = data.get('profile')
    profile_base = path.parent
    if isinstance(profile, str):
        profile_path = _path(profile, path.parent)
        profile = _json(_read(profile_path, sources, 'preparation_profile', limit=MAX_MANIFEST_BYTES))
        profile_base = profile_path.parent
    _require(isinstance(profile, dict), 'Preparation profile must be an object')
    _read(_path(data.get('structure'), path.parent), sources, 'preparation_structure')
    overrides = data.get('overrides', {})
    _require(isinstance(overrides, dict) and not set(overrides) - {'incar', 'kpoints', 'potentials'}
             and all(isinstance(value, dict) for value in overrides.values()),
             'Preparation overrides must contain only INCAR, kpoints and potential objects')
    potentials = profile.get('potentials', {})
    override_potentials = overrides.get('potentials', {})
    _require(isinstance(potentials, dict) and isinstance(override_potentials, dict), 'Explicit potentials required')
    potentials = {**potentials, **override_potentials}
    root = _path(potentials.get('root'), path.parent if 'root' in override_potentials else profile_base)
    variants = potentials.get('variants')
    _require(isinstance(variants, dict) and 0 < len(variants) <= 118, 'Bounded potential variants required')
    for variant in variants.values():
        _require(isinstance(variant, str) and Path(variant).name == variant and variant not in ('.', '..'),
                 'Potential variant must be one library directory name')
        _read(root / variant / 'POTCAR', sources, 'selected_potential')
    _require('source_record' not in data, 'Study uses explicitly pinned reviewed POSCARs; source_record inheritance is not supported')
    # Baseline authority belongs to the pinned study manifest, never an optional
    # mutable baseline in a preparation file.
    spec = load_spec(path)
    _require(spec.data['calculation'] == 'static', 'First study delivery supports fixed-geometry static VASP only')
    return spec


def _verify_sources(sources):
    current = {}
    for source in sources:
        data = _read(Path(source['path']), current, 'reverification')
        _require(content_identity(data) == {key: source[key] for key in ('sha256', 'size_bytes')},
                 f"Study source changed since approval: {source['path']}")


def _resolve(spec_path):
    sources = {}
    path = _path(spec_path)
    manifest = _object(_json(_read(path, sources, 'study_manifest', limit=MAX_MANIFEST_BYTES)),
                       _FIELDS, _REQUIRED, 'study manifest')
    _require(type(manifest['schema']) is int and manifest['schema'] == 1 and manifest['engine'] == 'VASP',
             'Study schema 1 and VASP engine required')
    _require(isinstance(manifest['study_id'], str) and re.fullmatch('[0-9a-f]{32}', manifest['study_id']),
             'study_id must be an explicit lowercase 32-hex UUID namespace')
    _require(isinstance(manifest['name'], str) and 0 < len(manifest['name']) <= 160, 'Study name required')
    for key in ('scientific_policy', 'metric', 'normalization', 'comparison_rule', 'execution'):
        _require(isinstance(manifest[key], dict) and bool(manifest[key]), f'Explicit {key} object required')
    varied = manifest['varied']
    _require(isinstance(varied, list) and all(isinstance(item, str) for item in varied)
             and len(set(varied)) == len(varied) and not set(varied) - VARIED_FIELDS,
             'varied supports ENCUT, k_sampling and explicit reviewed structure differences only')
    declarations, structures, meshes = _matrix(manifest)
    policy = _object(manifest['scientific_policy'], {'path', 'sha256'}, {'path', 'sha256'},
                     'reviewed scientific policy')
    policy_path = _path(policy['path'], path.parent)
    policy_data = _read(policy_path, sources, 'scientific_policy', limit=MAX_MANIFEST_BYTES)
    _require(isinstance(policy['sha256'], str) and re.fullmatch('[0-9a-f]{64}', policy['sha256'])
             and hashlib.sha256(policy_data).hexdigest() == policy['sha256'],
             'Scientific policy differs from reviewed bytes')
    policy['path'] = str(policy_path)
    baseline = _object(manifest['baseline'], {'directory', 'inputs'}, {'directory', 'inputs'}, 'baseline')
    _require(isinstance(baseline['inputs'], dict) and set(baseline['inputs']) == set(INPUT_NAMES),
             'Baseline must pin all four reviewed input identities')
    baseline_dir = _path(baseline['directory'], path.parent)
    old = {}
    for name in INPUT_NAMES:
        old[name] = _read(baseline_dir / name, sources, 'baseline:' + name)
        _identity(baseline['inputs'][name], old[name], 'Baseline ' + name)
    checked = check_input_bytes(old)
    _require(checked['status'] == 'valid', 'Baseline is outside the supported valid input dialect')
    # Unknown tags retain an unassessed plan, with materialization blocked.
    preparation_path = _path(manifest['preparation_spec'], path.parent)
    preparation = _bound_preparation(preparation_path, sources)
    output_root = _path(manifest['output_root'], path.parent)
    scratch_root = _path(manifest['scratch_root'], path.parent)
    record_root = _path(manifest['record_root'], path.parent)
    _require(all(item.is_dir() for item in (output_root, scratch_root, record_root))
             and record_root != scratch_root and record_root.is_relative_to(scratch_root),
             'Existing output root and record root strictly inside explicit Scratch required')
    baseline['directory'] = str(baseline_dir)
    manifest.update(preparation_spec=str(preparation_path), output_root=str(output_root),
                    scratch_root=str(scratch_root), record_root=str(record_root))
    if 'scratch_mount' in manifest:
        manifest['scratch_mount'] = str(_path(manifest['scratch_mount'], path.parent))
    for value in structures.values():
        location = _path(value['path'], path.parent)
        data = _read(location, sources, 'reviewed_structure:' + value['name'])
        _require(hashlib.sha256(data).hexdigest() == value['sha256'],
                 f"Reviewed structure changed: {value['name']}")
        value['path'] = str(location)
    cases, resolved = [], []
    for declaration in declarations:
        name = declaration['name']
        case_id = stable_hash({'study_id': manifest['study_id'], 'name': name})
        data = deepcopy(preparation.data)
        data.pop('baseline', None)
        data['structure'] = structures[declaration['structure']]['path']
        overrides = data.setdefault('overrides', {})
        incar_overrides = overrides.setdefault('incar', {})
        for key in list(incar_overrides):
            if key.upper() == 'ENCUT':
                del incar_overrides[key]
        incar_overrides['ENCUT'] = declaration['encut']
        overrides['kpoints'] = {key: value for key, value in meshes[declaration['kmesh']].items() if key != 'name'}
        derived = PreparationSpec(preparation.path, data, preparation.profile,
                                  preparation.profile_base, preparation.sources)
        files, record, publication = resolve_preparation_spec(derived, output=output_root / name,
            scratch_root=scratch_root, record_directory=record_root / name,
            scratch_mount=manifest.get('scratch_mount'))
        publication = publication_plan(output=publication['input_path'], scratch_root=scratch_root,
            record_directory=publication['record_directory'], scratch_mount=manifest.get('scratch_mount'),
            sources=[*publication['protected_sources'], str(baseline_dir), *sources])
        _require(files['POTCAR'] == old['POTCAR'], 'Every case must preserve exact reviewed potential dataset bytes and species block order')
        # Freeze one shared immutable potential snapshot across the bounded matrix.
        files['POTCAR'] = old['POTCAR']
        comparison = compare_inputs(old, files, prepared_runtime=record['expected_runtime'])
        comparison['study_fields'] = [{**change, 'study_field': _field(change)} for change in comparison['changes']]
        for change in comparison['study_fields']:
            field = change['study_field']
            if field in {'missing_evidence', 'title_comment_format'}:
                continue
            _require(field in varied, f'Case {name} changes fixed {field}; only explicitly declared study fields may vary')
        case = {'case_id': case_id, 'name': name,
                'variables': {'ENCUT': declaration['encut'], 'kmesh': declaration['kmesh'],
                              'structure': declaration['structure']},
                'output': publication['input_path'], 'record_directory': publication['record_directory'],
                'prepared_inputs': record['prepared_inputs'],
                'preparation': {key: record[key] for key in (
                    'schema_version', 'record_kind', 'preparation_content_id', 'calculation',
                    'prepared_inputs', 'resolved_settings', 'expected_runtime', 'validation', 'structure')},
                'comparison': comparison, 'semantic_identity': _semantic(files),
                'publication_plan': publication}
        cases.append(case)
        resolved.append((files, record, publication))
    reference = manifest['comparison_rule'].get('reference_case')
    matches = [case for case in cases if isinstance(reference, str)
               and reference in (case['case_id'], case['name'])]
    _require(len(matches) == 1, 'Comparison reference must identify exactly one declared case name or identity')
    manifest['comparison_rule']['reference_case'] = matches[0]['case_id']
    all_sources = sorted(sources.values(), key=lambda value: value['path'])
    _verify_sources(all_sources)
    assessed = all(case['comparison']['prepared_input_comparison_complete'] for case in cases)
    graph = WorkflowGraph('study-' + manifest['study_id'], tuple(CalculationNode(
        case['case_id'], operation='vasp-static', configuration={
            'study_case': case['name'], 'prepared_inputs': case['prepared_inputs']}) for case in cases),
        provenance={'study_id': manifest['study_id'], 'scheduler': 'existing Jobs; separate held registration'})
    plan = {'schema': 1, 'kind': 'cmw-convergence-study-plan', 'study_id': manifest['study_id'],
            'name': manifest['name'], 'engine': 'VASP', 'status': 'READY' if assessed else 'UNASSESSED',
            'case_count': len(cases), 'case_cap': MAX_CASES, 'spec': manifest, 'spec_path': str(path),
            'sources': all_sources, 'baseline': baseline, 'varied': varied,
            'scientific_policy': manifest['scientific_policy'], 'metric': manifest['metric'],
            'normalization': manifest['normalization'], 'comparison_rule': manifest['comparison_rule'],
            'cases': cases, 'workflow_graph': graph.to_dict(),
            'materializer': 'cmw.periodic.vasp.preparation standalone split publisher',
            'limitations': ['No generic VASP ExecutionRenderer or second scheduler is introduced.',
                'Unknown baseline tags leave fixed-category comparisons unassessed and block materialization.',
                'Prepared settings do not establish convergence or equivalent physical/magnetic states.']}
    plan['digest'] = stable_hash(plan)
    return plan, resolved


def plan_study(spec_path):
    """Return the entire explicit, deterministic study without creating any files."""
    return _resolve(spec_path)[0]


def materialize_study(spec_path, approved_digest):
    """Publish separate four-input cases; failures retain per-case split evidence."""
    plan, resolved = _resolve(spec_path)
    _require(plan['status'] == 'READY', 'Unassessed fixed categories block study materialization')
    _require(isinstance(approved_digest, str) and plan['digest'] == approved_digest,
             'Study changed since approval; review a fresh plan')
    receipts = []
    for case, (files, record, publication) in zip(plan['cases'], resolved):
        receipt = {'case_id': case['case_id'], 'name': case['name'], 'status': 'PENDING',
                   'output': case['output'], 'record_path': publication['record_path']}
        receipts.append(receipt)
        if any(item['status'] == 'FAILED' for item in receipts):
            continue
        try:
            _verify_sources(plan['sources'])
            record = {**record, 'study': {'study_id': plan['study_id'], 'case_id': case['case_id'],
                       'name': case['name'], 'planner_digest': plan['digest'], 'variables': case['variables']}}
            published = publish_preparation(files, record, publication)
            _verify_sources(plan['sources'])
            receipt.update(status='MATERIALIZED', publication=published['publication'],
                           prepared_inputs=published['prepared_inputs'], preparation_id=published['preparation_id'])
        except (OSError, ValueError) as exc:
            receipt.update(status='FAILED', error=str(exc), code=getattr(exc, 'code', 'STUDY_PUBLICATION_FAILED'),
                           incomplete=bool(getattr(exc, 'incomplete', False)))
    return {'schema': 1, 'kind': 'cmw-convergence-study-materialization', 'study_id': plan['study_id'],
            'planner_digest': plan['digest'], 'status': 'MATERIALIZED' if all(
                item['status'] == 'MATERIALIZED' for item in receipts) else 'PARTIAL', 'cases': receipts}
