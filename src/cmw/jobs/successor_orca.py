"""Geometry-only ORCA successors through the existing checked plan materializer.

Structural validation of a new input is separate from optimized-result acceptance.
The caller owns exact Jobs lifecycle closure, consent, publication and registration.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import re

from cmw.core.execution_profiles import load_execution_profiles
from cmw.core.execution_layout import ExecutionLayout, execution_target_directory_v2, next_attempt_identifier
from cmw.core.plan_materialization import ExecutionPlan, MaterializedExecutionNode, WorkflowPlanMaterializer
from cmw.core.provenance import stable_hash
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.core.workflow_graph import CalculationNode, WorkflowGraph
from cmw.molecular.orca import jobs as orca_jobs
from cmw.molecular.orca.input import resolve_orca_resources
from cmw.molecular.orca.job import check_reuse, load_target
from cmw.molecular.orca.geometry import OrcaGeometryInput, validate_orca_geometry_input
from cmw.molecular.orca.renderer import OrcaExecutionRenderer, orca_execution_plan_node
from cmw.molecular.orca.status import FINAL_ENERGY_RE, NUMBER, StageType, parse_orca_output, validate_stage
from cmw.structure.xyz import Atom, XYZGeometry, geometry_hash, read_xyz
from cmw.progress.orca import CYCLE
from .review_engines import _Sources, _manifest_sources, _molecular_binding
from .runner_assets import file_identity

_LIMIT = 64 * 1024 * 1024
_COORDINATES = re.compile(r'^\s*CARTESIAN COORDINATES\s*\(ANGSTROEM\)\s*$', re.I)
_ROW = re.compile(rf'^\s*([A-Z][a-z]?)\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*$')


def _path(value, label):
    if not isinstance(value, str) or not Path(value).is_absolute() or Path(value).resolve() != Path(value):
        raise ValueError(f'{label} requires an original canonical absolute path')
    return Path(value)


def _flag(argv, flag):
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 == len(argv):
        raise ValueError(f'Parent has no unambiguous registered {flag}')
    return argv[positions[0] + 1]


def _evaluated_frames(text, elements):
    """Read only complete ordinary angstrom tables followed by native energy.

    No geometry is selected by file age, trailing coordinates, or normal termination.
    A later incomplete/unevaluated table cannot certify the candidate it contains.
    """
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if _COORDINATES.fullmatch(line)]
    frames = []
    for ordinal, start in enumerate(headers, 1):
        end = headers[ordinal] if ordinal < len(headers) else len(lines)
        end = next((i for i in range(start + 1, end) if CYCLE.search(lines[i])), end)
        index = start + 1
        while index < end and (not lines[index].strip() or set(lines[index].strip()) == {'-'}):
            index += 1
        atoms, precision = [], []
        while index < end and (match := _ROW.fullmatch(lines[index])):
            values = tuple(float(v.replace('D', 'E').replace('d', 'e')) for v in match.groups()[1:])
            atoms.append(Atom(match[1], *values))
            precision.append(tuple(Decimal(v.replace('D', 'E').replace('d', 'e')).as_tuple().exponent
                                   for v in match.groups()[1:]))
            index += 1
        if (len(atoms) != len(elements) or tuple(a.element for a in atoms) != tuple(elements)
                or index >= end or lines[index].strip()
                or any(exponent > -6 for row in precision for exponent in row)):
            continue
        energy = next((j for j in range(index, end) if FINAL_ENERGY_RE.fullmatch(lines[j])), None)
        if energy is None:
            continue
        frames.append({'frame': ordinal, 'line': start + 1, 'energy_line': energy + 1,
                       'geometry': XYZGeometry(tuple(atoms)), 'precision': precision})
    return frames


def _matches_frame(candidate, frame):
    if candidate.elements != frame['geometry'].elements:
        return False
    return all(abs(a - b) <= 0.500001 * 10 ** exponent
               for atom, observed, precision in zip(candidate.atoms, frame['geometry'].atoms, frame['precision'], strict=True)
               for a, b, exponent in zip(atom.coordinates, observed.coordinates, precision, strict=True))


def _change(changes, category, field, before, after):
    if before != after:
        changes.append({'category': category, 'field': field, 'before': before, 'after': after})


def _settings(parsed):
    words = parsed['spec'].keywords.split()
    physical = sorted(v.casefold() for v in words if v.casefold() in orca_jobs._METHODS | orca_jobs._BASES
                      or v.casefold() in {'rijcosx', 'rijk', 'd3bj', 'd4'})
    numeric = sorted(v.casefold() for v in words if v.casefold() not in set(physical) | {'noautostart'})
    return physical, {'keywords': numeric, 'blocks': [' '.join(b.casefold().split()) for b in parsed['spec'].blocks]}


def _prepare_preview(options, parent_target, *, job, state_directory, runtime, resources, published_layout=None):
    if not isinstance(options, dict) or set(options) != {'source', 'template_input', 'execution_config', 'execution_profile', 'output'}:
        raise ValueError('ORCA adapter requires source, template_input, execution_config, execution_profile and output')
    source = options['source']
    if not isinstance(source, dict) or set(source) - {'mode', 'input', 'target', 'geometry', 'output', 'result', 'evaluation'}:
        raise ValueError('Unsupported ORCA source fields')
    if not {'mode', 'input', 'target', 'geometry'} <= set(source) or source['mode'] not in {'input', 'observed', 'accepted'}:
        raise ValueError('ORCA source requires explicit input, observed or accepted geometry mode')
    if source['mode'] != 'input' and 'output' not in source:
        raise ValueError('Observed/accepted geometry requires its exact registered native output')
    if source['mode'] == 'accepted' and 'result' not in source:
        raise ValueError('Accepted geometry requires its exact original finalizer record')
    if 'evaluation' in source and (type(source['evaluation']) is not int or source['evaluation'] < 1):
        raise ValueError('evaluation must be a positive native Cartesian frame index')
    if job.get('engine') != 'ORCA' or job.get('id') != parent_target.get('job_id') or job.get('attempt_id') != parent_target.get('attempt_id'):
        raise ValueError('ORCA source must belong to the exact selected Jobs attempt')
    sources = _Sources(_LIMIT, None)
    binding = _molecular_binding(job, sources)
    association = binding['plan']['scientific_association']
    if association.get('workflow_state') or association.get('stage') not in {'SP', 'OPT'}:
        raise ValueError('Successors support only ordinary registered single-stage ORCA SP/OPT parents')
    stage = StageType(association['stage'])
    argv = binding['plan']['argv']
    for field, flag in (('input', '--input'), ('target', '--target'), ('output', '--output')):
        if field in source and str(_path(source[field], field)) != _flag(argv, flag):
            raise ValueError(f'Selected {field} differs from the registered parent source')
    if 'result' in source and source['result'] != binding['plan']['result_path']:
        raise ValueError('Selected result differs from the original parent finalizer record')
    for field in ('input', 'target', 'geometry', 'output', 'result'):
        if field in source:
            sources.observe(_path(source[field], field), 'orca_successor:' + field)
    parent = orca_jobs.parse_input(Path(source['input']), stage)
    parent_science, _ = load_target(Path(source['target']))
    if (parent_science.target_id != association.get('target_id')
            or parent_science.geometry_sha256 != geometry_hash(parent['geometry'])
            or (parent_science.charge, parent_science.multiplicity) != (parent['charge'], parent['multiplicity'])):
        raise ValueError('Prepared parent target/input identity differs from the registered association')
    candidate = read_xyz(Path(source['geometry']))
    if candidate.elements != parent['geometry'].elements:
        raise ValueError('Selected geometry does not preserve the exact parent atom/species order')
    allowed_geometry = {str(path) for path in parent['dependencies'][1:]}
    if source['mode'] != 'input':
        allowed_geometry.update(item.split('=', 1)[1] for i, item in enumerate(argv)
                                if i and argv[i - 1] == '--artifact' and item.startswith('final_geometry='))
    if source['geometry'] not in allowed_geometry:
        raise ValueError('Geometry is not an exact registered input or declared final_geometry artifact')
    selected_frame = None
    qualification = 'REVIEWED_INPUT_GEOMETRY'
    if source['mode'] == 'input':
        if geometry_hash(candidate) != parent_science.geometry_sha256:
            raise ValueError('Retry input geometry differs from the registered parent input')
    else:
        text = Path(source['output']).read_text(encoding='utf-8')
        native = parse_orca_output(text)
        if native.orca_version is None or not native.orca_version.startswith('6.1.'):
            raise ValueError('Observed geometry requires the bounded ORCA 6.1.x native dialect')
        frames = _evaluated_frames(text, candidate.elements)
        matching = [frame for frame in frames if _matches_frame(candidate, frame)]
        if 'evaluation' in source:
            matching = [frame for frame in matching if frame['frame'] == source['evaluation']]
        elif frames:
            matching = [frame for frame in matching if frame['frame'] == frames[-1]['frame']]
        if len(matching) != 1:
            raise ValueError('Selected geometry is not a complete, unambiguous evaluated native Cartesian frame')
        selected_frame = {k: matching[0][k] for k in ('frame', 'line', 'energy_line')}
        qualification = 'COMPLETE_OBSERVED_GEOMETRY_NOT_OPTIMIZED'
        if source['mode'] == 'accepted':
            record = sources.json(Path(source['result']), 'orca_finalizer_record')
            _manifest_sources(Path(source['result']), record, sources)
            if not check_reuse(Path(source['target']), Path(source['result']))['reuse']:
                raise ValueError('Existing ORCA finalizer/reuse contract does not accept this exact source')
            assessed = validate_stage(native, stage)
            if not assessed.valid or (stage is StageType.OPT and not native.optimization_converged):
                raise ValueError('Normal termination alone does not establish optimized ORCA geometry')
            if stage is StageType.OPT:
                artifact = record.get('artifacts', {}).get('final_geometry', {})
                if artifact.get('sha256') != sources.records[source['geometry']]['sha256']:
                    raise ValueError('Accepted candidate differs from the finalized geometry bytes')
            qualification = 'FINALIZED_OPTIMIZED_GEOMETRY' if stage is StageType.OPT else 'ACCEPTED_SINGLE_POINT_INPUT_GEOMETRY'
    template = _path(options['template_input'], 'template_input')
    sources.observe(template, 'successor_template')
    # Explicit stage is required in the existing bounded parser, never chosen here.
    stage_words = re.findall(r'^\s*!([^\n]+)', template.read_text(), re.M)
    selected_stages = [word.upper() for line in stage_words for word in line.split() if word.casefold() in {'sp', 'opt', 'freq'}]
    if len(selected_stages) != 1 or selected_stages[0] not in {'SP', 'OPT'}:
        raise ValueError('Successor template requires exactly one ordinary SP or OPT stage')
    next_stage = StageType(selected_stages[0])
    successor = orca_jobs.parse_input(template, next_stage)
    for path in successor['dependencies']:
        sources.observe(path, 'successor_template')
    if successor['geometry'].elements != candidate.elements:
        raise ValueError('Successor template must preserve the parent atom/species order')
    if geometry_hash(successor['geometry']) != geometry_hash(candidate):
        raise ValueError('Successor template coordinates must exactly equal the explicitly selected geometry')
    if 'noautostart' not in {word.casefold() for word in successor['spec'].keywords.split()}:
        raise ValueError('Geometry-only successor template must explicitly declare NoAutoStart; GBW/checkpoint reuse is unsupported')
    config = _path(options['execution_config'], 'execution_config')
    sources.observe(config, 'execution_profile')
    profiles = load_execution_profiles(config)
    if options['execution_profile'] not in profiles.profiles:
        raise ValueError('Explicit execution_profile is not present in execution_config')
    profile = profiles.profiles[options['execution_profile']]
    resolved = resolve_orca_resources(profile)
    if resolved.resources != successor['resources']:
        raise ValueError('Explicit profile PAL/MaxCore differs from the reviewed input template; no resource rewrite is permitted')
    orca_jobs._resources(resources, successor['resources'])
    executable, runtime_dependencies = orca_jobs._runtime(runtime, successor['resources'])
    for item in runtime_dependencies:
        sources.observe(item['path'], 'successor_runtime')
    output = _path(options['output'], 'output')
    state = Path(state_directory).resolve()
    parent_directory = Path(binding['plan']['cwd'])
    if (output.exists() and published_layout is None) or output == state or output.is_relative_to(state) or output == parent_directory or output.is_relative_to(parent_directory):
        raise ValueError('ORCA output requires a new no-clobber destination outside the parent and Jobs state')
    artifact = structure_artifact_from_file(Path(source['geometry']), source='reviewed_orca_successor_geometry',
        producing_calculation=stable_hash(parent_target), charge=parent['charge'], multiplicity=parent['multiplicity'],
        provenance={'parent': parent_target, 'qualification': qualification},
        metadata={'parent': parent_target, 'qualification': qualification})
    node = orca_execution_plan_node('successor', successor['spec'], input_artifacts=(artifact,),
        geometry_artifact_id=artifact.artifact_id,
        geometry_selection={'charge': successor['charge'], 'multiplicity': successor['multiplicity'],
                            'allow_electronic_state_override': True},
        outputs={'final_geometry': 'stage.xyz'} if next_stage is StageType.OPT else {},
        lineage={'source': 'reviewed_orca_successor_geometry', 'parent_stage': stage.value,
                 'parent_target_id': parent_science.target_id, 'parent_artifact_sha256': geometry_hash(candidate)})
    plan = ExecutionPlan(WorkflowGraph('reviewed-successor', (CalculationNode('successor'),)), (node,),
                         provenance={'parent': parent_target, 'qualification': qualification})
    target = OrcaExecutionRenderer().build_target(node, (artifact,))
    target_directory = execution_target_directory_v2(output, target_identifier=target.target_id,
                                                     short_id_width=plan.short_target_id_width)
    attempt = (next_attempt_identifier(target_directory) if published_layout is None
               else ExecutionLayout.from_mapping(published_layout, root_override=output).attempt_identifier)
    layout = ExecutionLayout(output, 'successor', 'successor', target.target_id, attempt,
                             version=plan.layout_version, short_id_width=plan.short_target_id_width)
    if published_layout is not None and layout.to_dict() != published_layout:
        raise ValueError('Published layout differs from the exact approved ORCA destination')
    changes = []
    _change(changes, 'geometry/cell', 'geometry_sha256', parent_science.geometry_sha256, geometry_hash(candidate))
    _change(changes, 'scientific_stage', 'stage', stage.value, next_stage.value)
    _change(changes, 'Hamiltonian/spin/charge', 'charge', parent['charge'], successor['charge'])
    _change(changes, 'Hamiltonian/spin/charge', 'multiplicity', parent['multiplicity'], successor['multiplicity'])
    old_physical, old_numeric = _settings(parent)
    new_physical, new_numeric = _settings(successor)
    _change(changes, 'Hamiltonian/spin/charge', 'method_basis_dispersion', old_physical, new_physical)
    _change(changes, 'solver/convergence controls', 'numerical_settings', old_numeric, new_numeric)
    _change(changes, 'resources', 'PAL_MaxCore', parent['resources'].to_dict(), successor['resources'].to_dict())
    _change(changes, 'resources', 'Jobs_commitments', job['resources'], resources)
    _change(changes, 'checkpoint semantics', 'NoAutoStart', 'noautostart' in parent['spec'].keywords.casefold().split(), True)
    sources.verify_stable()
    source_records = [{'path': path, 'sha256': row['sha256'], 'size_bytes': row['bytes_read'], 'stat': row['stat_after'], 'role': row['role']}
                      for path, row in sorted(sources.records.items())]
    preview = {'schema': 1, 'engine': 'ORCA', 'parent': parent_target, 'source_qualification': qualification,
        'selected_frame': selected_frame, 'changes': changes, 'source_records': source_records,
        'source_geometry_sha256': geometry_hash(candidate), 'target_id': target.target_id,
        'execution_plan': plan.to_dict(), 'publication_layout': layout.to_dict(), 'resource_profile': profile.to_dict(), 'output': str(output),
        'checkpoint': {'mode': 'none', 'status': 'REFUSED', 'reason': 'Electronic GBW/checkpoint reuse has no complete supported successor contract'},
        'constraints': 'Ordinary Cartesian all-electron input; constrained/ghost/ECP/fragment constructs refused',
        'runtime': runtime, 'resources': resources}
    return preview, plan, profile, successor, executable


def preview_orca(options, parent_target, *, job, state_directory, runtime, resources):
    return _prepare_preview(options, parent_target, job=job, state_directory=state_directory, runtime=runtime, resources=resources)[0]


def _verify_source_records(records):
    sources = _Sources(_LIMIT, None)
    for expected in records:
        actual = sources.observe(expected['path'])
        if (actual['sha256'] != expected['sha256'] or actual['stat_after'] != expected['stat']):
            raise ValueError('ORCA source changed during materialization: ' + expected['path'])
    sources.verify_stable()


def _runtime_identity(runtime, executable):
    return {'orca': {'path': str(executable), 'sha256': runtime['sha256'], 'version': runtime['version']},
            'qualification': runtime['qualification']}


def _prepared_execution(record, parsed, preview, runtime, resources):
    layout = record.layout
    primary = str(layout.input_path('stage.inp'))
    generated = orca_jobs.parse_input(_path(primary, 'materialized input'), parsed['spec'].stage_type)
    if (generated['spec'].scientific_identity() != parsed['spec'].scientific_identity()
            or generated['resources'] != parsed['resources']
            or geometry_hash(generated['geometry']) != preview['source_geometry_sha256']
            or (generated['charge'], generated['multiplicity']) != (parsed['charge'], parsed['multiplicity'])):
        raise ValueError('Materialized ORCA input differs from its approved scientific/template contract')
    scientific = {'profile': parsed['spec'].stage_type.value.lower(), 'input': primary}
    partial = {'workdir': str(layout.working_directory), 'scientific': scientific, 'runtime': runtime, 'resources': resources}
    checked = orca_jobs.check(partial)
    if checked['status'] != 'READY':
        raise ValueError('Complete materialized ORCA input check failed: ' + json.dumps(checked['findings']))
    prepared = checked['plan']
    target_path = str(layout.target_directory / 'target.json')
    index = prepared['argv'].index('--target')
    prepared['argv'][index + 1] = target_path
    prepared['argv'].extend(['--layout', str(layout.layout_path)])
    prepared['scientific_association'].update(target_id=record.target_id, geometry_sha256=preview['source_geometry_sha256'],
                                              attempt_id=record.attempt.attempt_id)
    paths = {primary, str(layout.input_path('input.xyz')), target_path, str(layout.layout_path),
             *(str(layout.metadata_path(name)) for name in ('geometry-input.json', 'orca-runtime.json',
                 'execution-attempt.json', 'materialization.json'))}
    prepared['dependencies'].extend(file_identity(_path(path, 'prepared dependency')) for path in sorted(paths))
    prepared['dependencies'] = list({row['path']: row for row in prepared['dependencies']}.values())
    return {'materialization': record.to_dict(), 'prepared_plan': prepared, 'scientific': scientific, 'workdir': preview['output']}


def verify_materialized_orca(options, parent_target, approved_preview, materialized, *, job, state_directory, runtime, resources):
    """Reconstruct execution authority from the approved input and original layout.

    A mutable publication record cannot authorize arbitrary argv, environment,
    runtime, paths or sources merely by naming a once-approved plan digest.
    """
    preview, plan, profile, parsed, executable = _prepare_preview(options, parent_target, job=job,
        state_directory=state_directory, runtime=runtime, resources=resources,
        published_layout=approved_preview['publication_layout'])
    if preview != approved_preview:
        raise ValueError('ORCA approved plan/source changed before registration')
    layout = ExecutionLayout.from_mapping(preview['publication_layout'], root_override=Path(options['output']))
    sources = _Sources(_LIMIT, None)
    raw = sources.json(layout.metadata_path('materialization.json'), 'original_materialization')
    record = MaterializedExecutionNode.from_mapping(raw, root_override=Path(options['output']))
    node = plan.nodes[0]
    expected_target = OrcaExecutionRenderer().build_target(node, node.input_artifacts)
    expected_runtime = _runtime_identity(runtime, executable)
    expected_outputs = {'output': str(layout.output_path('stage.out'))}
    if parsed['spec'].stage_type is StageType.OPT:
        expected_outputs['final_geometry'] = str(layout.output_path('stage.xyz'))
    expected_provenance = {'layout': str(layout.layout_path),
        'geometry_contract': str(layout.metadata_path('geometry-input.json')),
        'runtime_contract': str(layout.metadata_path('orca-runtime.json'))}
    if (record.layout != layout or record.execution_plan_id != plan.execution_plan_id
            or record.target != expected_target.to_dict() or record.execution_intent != node.execution_intent
            or record.node_id != 'successor' or record.renderer_id != 'orca'
            or record.status.value != 'PREPARED' or record.planned_artifact_ids
            or record.resource_profile_hash != profile.execution_profile_hash
            or record.runtime_identity_hash != stable_hash(expected_runtime)
            or record.input_files != {'primary': str(layout.input_path('stage.inp')), 'geometry': str(layout.input_path('input.xyz'))}
            or record.output_files != expected_outputs or record.log_files != {'stderr': str(layout.log_path('stage.err'))}
            or record.provenance_files != expected_provenance):
        raise ValueError('Original materializer record differs from the approved execution contract')
    target, _ = load_target(_path(str(layout.target_directory/'target.json'), 'materialized target'))
    geometry = OrcaGeometryInput.from_mapping(sources.json(layout.metadata_path('geometry-input.json')))
    validate_orca_geometry_input(geometry)
    if (target != expected_target or geometry.source_artifact.to_dict() != node.input_artifacts[0].to_dict()
            or geometry.input_path != layout.input_path('input.xyz')
            or geometry.charge != parsed['charge'] or geometry.multiplicity != parsed['multiplicity']
            or tuple(geometry.atom_indices) != tuple(range(geometry.atom_count))
            or tuple(geometry.atom_labels) != parsed['geometry'].elements
            or sources.json(layout.metadata_path('orca-runtime.json')) != expected_runtime
            or sources.json(layout.layout_path) != layout.to_dict()
            or sources.json(layout.metadata_path('execution-attempt.json')) != record.attempt.to_dict()
            or record.attempt.resources != parsed['resources'].to_dict()
            or record.attempt.executable != expected_runtime['orca']
            or record.attempt.generated_input_sha256 != file_identity(layout.input_path('stage.inp'))['sha256']):
        raise ValueError('Materialized geometry, target, runtime or attempt evidence differs from approved input')
    verified = _prepared_execution(record, parsed, preview, runtime, resources)
    sources.verify_stable()
    _verify_source_records(preview['source_records'])
    if verified != materialized:
        raise ValueError('Stored ORCA execution differs from the rederived approved materialization')
    return verified


def materialize_orca(options, parent_target, approved_preview, *, job, state_directory, runtime, resources):
    preview, plan, profile, parsed, executable = _prepare_preview(options, parent_target, job=job,
        state_directory=state_directory, runtime=runtime, resources=resources)
    if preview != approved_preview:
        raise ValueError('ORCA approved plan or source bytes changed before materialization')
    output = Path(options['output'])
    output.mkdir(mode=0o700)
    try:
        record = WorkflowPlanMaterializer({'orca': OrcaExecutionRenderer()}).materialize_node(plan, 'successor',
            project_root=output, system_identifier='successor', resource_profile=profile,
            runtime_identity=_runtime_identity(runtime, executable))
        if record.layout.to_dict() != preview['publication_layout']:
            raise ValueError('Materializer destination differs from approved publication plan')
        materialized = _prepared_execution(record, parsed, preview, runtime, resources)
        _verify_source_records(preview['source_records'])
        return materialized
    except Exception as exc:
        with (output / 'successor-failure.json').open('x') as stream:
            json.dump({'schema': 1, 'status': 'INCOMPLETE', 'parent': parent_target,
                       'approved_plan_digest': stable_hash(approved_preview), 'error': str(exc)[:2048]}, stream, indent=2)
        raise
