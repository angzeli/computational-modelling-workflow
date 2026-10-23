"""Bounded, read-only ORCA Jobs preflight and intentional preparation.

The imported dialect is ordinary SP/OPT/FREQ, Cartesian angstrom xyz/xyzfile,
explicit PAL/MaxCore, a small keyword vocabulary and MaxIter-only SCF/geometry
blocks. Unsupported constructs are not declared scientifically invalid. Original
input bytes are retained; preparation renders a separate canonical attempt using
CMW's existing target, geometry, runner and finalization contracts.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys

from cmw.core.job import GeometryLineage
from cmw.core.provenance import atomic_write_json, file_hash
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.structure.xyz import Atom, XYZGeometry, format_xyz, geometry_hash, read_xyz, write_xyz
from .geometry import prepare_orca_geometry_input
from .input import (ORCA_MEMORY_MB_PER_GB, ORCA_MEMORY_SAFETY_FRACTION,
                    OrcaResources, OrcaStageSpec, make_target_from_geometry_input, render_orca_input)
from .job import write_target
from .runtime import inspect_recorded_orca_runtime
from .status import FrequencyPolicy, StageType

MAX_INPUT_BYTES = 1024 * 1024
_METHODS = {'hf', 'rhf', 'uhf', 'pbe', 'pbe0', 'b3lyp', 'b3lyp/g', 'bp86', 'blyp',
            'tpss', 'tpssh', 'r2scan', 'r2scan-3c', 'b97-3c', 'hf-3c'}
_BASES = {'sto-3g', 'def2-svp', 'def2-sv(p)', 'def2-tzvp', 'def2-tzvpp',
          'def2-qzvp', 'def2-qzvpp', 'def2/j', 'def2/jk', 'def2-svp/c',
          'def2-tzvp/c', 'def2-tzvpp/c', 'autoaux'}
_OPTIONS = {'tightscf', 'verytightscf', 'normalscf', 'loosescf',
            'slowconv', 'veryslowconv', 'soscf', 'nososcf', 'diis', 'nodiis',
            'tightopt', 'verytightopt', 'normalopt', 'looseopt', 'rijcosx', 'rijk',
            'd3bj', 'd4', 'grid4', 'grid5', 'grid6', 'defgrid1', 'defgrid2', 'defgrid3',
            'smallprint', 'normalprint', 'largeprint', 'noautostart', 'angstrom'}
# Explicit all-electron scope avoids claiming parity for ECP/ghost/fragment cases.
_ATOMIC_NUMBER = {element: index + 1 for index, element in enumerate(
    'H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn'.split())}
_ATOMIC_NUMBER.update({'Ga':31, 'Ge':32, 'As':33, 'Se':34, 'Br':35, 'Kr':36})


class Unsupported(ValueError):
    pass


class Unassessed(ValueError):
    pass


def _in_domain(domain, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if not hasattr(exc, 'preflight_domain'):
            exc.preflight_domain = domain
        raise


def _read(path: Path):
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f'Input is missing or exceeds 1 MiB: {path}')
    return path.read_text(encoding='utf-8')


def _path(value, label):
    if not isinstance(value, str) or not value or any(c in value for c in ('\0', '\n', '\r')):
        raise ValueError(f'{label} must be an explicit absolute path')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f'{label} must be absolute')
    return path.resolve()


def _dependency(path):
    from cmw.jobs.runner_assets import file_identity
    return file_identity(path)


def _keywords(keywords):
    tokens = keywords.split()
    unknown = [token for token in tokens if token.casefold() not in _METHODS | _BASES | _OPTIONS]
    if unknown:
        raise Unsupported('Unsupported ORCA keyword or dependency construct: ' + ', '.join(unknown))
    normalized = [token.casefold() for token in tokens]
    if len(normalized) != len(set(normalized)):
        raise ValueError('Duplicate ORCA keywords are outside the bounded input contract')
    methods = set(normalized) & (_METHODS - {'rhf','uhf'})
    references = set(normalized) & {'rhf','uhf'}
    if len(methods) > 1 or len(references) > 1:
        raise ValueError('Conflicting ORCA method/reference declarations')
    if not methods and not references:
        raise Unsupported('A supported explicit method is required; no method is selected automatically')
    orbital_bases = set(normalized) & {basis for basis in _BASES if '/' not in basis and basis != 'autoaux'}
    composite = any(token.endswith('-3c') for token in methods)
    if len(orbital_bases) > 1 or (composite and orbital_bases):
        raise Unsupported('Multiple or composite-method orbital-basis overrides are outside this input dialect')
    if not composite and not orbital_bases:
        raise Unsupported('This bounded imported dialect requires an explicit supported orbital basis; auxiliary bases do not supply one')
    for options in ({'soscf','nososcf'},{'diis','nodiis'},
                    {'tightscf','verytightscf','normalscf','loosescf'},
                    {'tightopt','verytightopt','normalopt','looseopt'}):
        if len(set(normalized) & options) > 1:
            raise ValueError('Conflicting ORCA convergence/algorithm declarations')


def _blocks(blocks):
    seen=set()
    for block in blocks:
        text = ' '.join(block.split())
        if not re.fullmatch(r'%(?:scf|geom)\s+MaxIter\s+[1-9]\d*\s+end', text, re.I):
            raise Unsupported('Unsupported ORCA block or dependency-bearing directive: ' + text[:160])
        kind=text.split()[0].casefold()
        if kind in seen:
            raise ValueError('Duplicate ORCA '+kind+' block')
        seen.add(kind)


def _electronic_state(geometry, charge, multiplicity):
    if type(charge) is not int or type(multiplicity) is not int or multiplicity < 1:
        raise ValueError('Charge must be an integer and multiplicity a positive integer')
    if any(atom.ghost or atom.element not in _ATOMIC_NUMBER for atom in geometry.atoms):
        raise Unsupported('First-class ORCA input is bounded to ordinary all-electron H–Kr atoms; ghost/ECP/fragment parity is unassessed')
    electrons = sum(_ATOMIC_NUMBER[atom.element] for atom in geometry.atoms) - charge
    if electrons < 1 or multiplicity - 1 > electrons or (electrons - (multiplicity - 1)) % 2:
        raise ValueError('Electron count and multiplicity are inconsistent for the supported all-electron geometry')


def parse_input(path: Path, stage: StageType):
    """Parse the documented bounded imported dialect without changing its bytes."""
    lines = []
    for raw in _read(path).splitlines():
        line = raw.split('#', 1)[0].strip()
        if line:
            lines.append(line)
    keywords, blocks, geometry, dependencies = [], [], None, [path]
    charge = multiplicity = pal = maxcore = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith('!'):
            keywords.extend(line[1:].split())
        elif re.match(r'%pal\b', line, re.I):
            if pal is not None:
                raise ValueError('Duplicate or conflicting PAL declarations')
            block = line
            while not re.search(r'\bend\s*$', block, re.I) and index + 1 < len(lines):
                index += 1
                block += ' ' + lines[index]
            match = re.fullmatch(r'%pal\s+nprocs\s+([1-9]\d*)\s+end', block, re.I)
            if not match:
                raise ValueError('Incomplete or unsupported PAL block')
            pal = int(match[1])
        elif re.match(r'%maxcore\b', line, re.I):
            if maxcore is not None:
                raise ValueError('Duplicate MaxCore declarations')
            match = re.fullmatch(r'%maxcore\s+([1-9]\d*)', line, re.I)
            if not match:
                raise ValueError('MaxCore must be an explicit positive integer MB per process')
            maxcore = int(match[1])
        elif re.match(r'%(?:scf|geom)\b', line, re.I):
            block = line
            while not re.search(r'\bend\s*$', block, re.I) and index + 1 < len(lines):
                index += 1
                block += ' ' + lines[index]
            _blocks((block,))
            blocks.append(block)
        elif line.startswith('*'):
            if geometry is not None:
                raise ValueError('Multiple geometry declarations are not supported')
            match = re.fullmatch(r'\*\s+xyz(file)?\s+(-?\d+)\s+([1-9]\d*)(?:\s+(.+))?', line, re.I)
            if not match:
                raise Unsupported('Only ordinary Cartesian xyz/xyzfile geometry in angstrom is supported')
            charge, multiplicity = int(match[2]), int(match[3])
            if match[1]:
                reference = match[4]
                if not reference:
                    raise ValueError('xyzfile requires an explicit geometry dependency')
                if reference.startswith('"') and reference.endswith('"'):
                    reference = reference[1:-1]
                relative = Path(reference)
                if relative.is_absolute() or '..' in relative.parts or '\0' in reference:
                    raise Unsupported('Imported xyzfile must reference a relative dependency within its input tree')
                geometry_path = (path.parent / relative).resolve()
                if not geometry_path.is_relative_to(path.parent.resolve()):
                    raise ValueError('Geometry dependency escapes its input tree')
                _read(geometry_path)
                geometry = read_xyz(geometry_path)
                dependencies.append(geometry_path)
            else:
                if match[4]:
                    raise ValueError('Inline xyz opener has unexpected trailing fields')
                atoms = []
                index += 1
                while index < len(lines) and lines[index] != '*':
                    fields = lines[index].split()
                    if len(fields) != 4:
                        raise ValueError('Inline geometry requires one element and three finite Cartesian coordinates')
                    try:
                        atoms.append(Atom(fields[0], *(float(x.replace('D', 'E').replace('d','e')) for x in fields[1:])))
                    except ValueError as exc:
                        raise ValueError(f'Invalid inline geometry: {exc}') from exc
                    index += 1
                if index >= len(lines):
                    raise ValueError('Incomplete inline xyz geometry: closing * is missing')
                geometry = XYZGeometry(tuple(atoms), 'Imported ORCA inline geometry; angstrom')
        else:
            raise Unsupported('Unsupported ORCA construct or dependency: ' + line[:160])
        index += 1
    stage_tokens = [token for token in keywords if token.casefold() in {'sp', 'opt', 'freq'}]
    if len(stage_tokens) != 1 or stage_tokens[0].upper() != stage.value:
        raise ValueError('Input requires exactly one explicit calculation stage matching the selected profile')
    pal_tokens = [token for token in keywords if re.fullmatch(r'pal\d+', token, re.I)]
    if pal_tokens:
        if len(pal_tokens) != 1 or pal is not None:
            raise ValueError('Duplicate or conflicting PAL keyword/block declarations')
        pal = int(pal_tokens[0][3:])
    keywords = ' '.join(token for token in keywords if token not in stage_tokens + pal_tokens)
    _keywords(keywords)
    _blocks(blocks)
    if geometry is None:
        raise ValueError('Input has no complete geometry')
    if pal is None or maxcore is None:
        raise ValueError('Explicit PAL process count and MaxCore are required')
    _electronic_state(geometry, charge, multiplicity)
    return {'spec': OrcaStageSpec(stage, keywords, tuple(blocks)), 'geometry': geometry,
            'charge': charge, 'multiplicity': multiplicity,
            'resources': OrcaResources(pal, maxcore), 'dependencies': dependencies}


def _resources(resources, selected):
    cpus, memory = resources.get('cpus'), resources.get('memory_gib')
    if type(cpus) is not int or cpus < 1 or type(memory) not in (int,float) or not math.isfinite(memory) or memory <= 0:
        raise ValueError('Explicit positive CPU and total GiB commitments are required')
    if cpus != selected.nprocs or resources.get('mpi_ranks', selected.nprocs) != selected.nprocs or resources.get('threads_per_rank',1) != 1:
        raise ValueError('Jobs CPU/rank/thread commitments contradict effective ORCA PAL processes')
    available_mb = math.floor(memory * ORCA_MEMORY_MB_PER_GB * ORCA_MEMORY_SAFETY_FRACTION)
    if selected.nprocs * selected.maxcore_mb_per_process > available_mb:
        raise ValueError('Total memory commitment understates MaxCore × PAL plus the existing 20% headroom policy')


def _runtime(runtime, selected):
    allowed = {'executable','sha256','version','qualification','contract'}
    if not isinstance(runtime, dict) or set(runtime) - allowed:
        raise ValueError('Unsupported ORCA runtime fields; the driver is invoked directly, never through mpirun')
    if not {'executable','sha256','version','qualification'} <= set(runtime):
        raise Unassessed('Recorded executable identity, version and qualification are required; no runtime probe was run')
    executable = _path(runtime['executable'],'runtime executable')
    if not executable.is_file() or not os.access(executable,os.X_OK):
        raise ValueError('Selected ORCA driver is unavailable or not executable')
    if executable.name in {'mpirun','mpiexec','orterun'}:
        raise ValueError('ORCA driver must not be wrapped in mpirun')
    if runtime['qualification'] not in {'recorded','synthetic'}:
        raise Unassessed('ORCA runtime qualification must be recorded or explicitly synthetic')
    if not isinstance(runtime['version'],str) or not re.fullmatch(r'6\.1(?:\.\d+)?',runtime['version']):
        raise Unsupported('First-class observation is bounded to recorded ORCA 6.1.x evidence')
    if executable.stat().st_size > 8 * 1024**3:
        raise Unsupported('ORCA driver identity exceeds the 8 GiB streaming bound')
    record = _dependency(executable)
    if record['sha256'] != runtime['sha256']:
        raise ValueError('Selected ORCA driver bytes disagree with recorded evidence')
    dependencies = [record]
    if runtime.get('contract'):
        contract = _path(runtime['contract'],'runtime contract')
        value = json.loads(_read(contract))
        dependencies.extend(inspect_recorded_orca_runtime(value, orca_executable=executable,nprocs=selected.nprocs))
        dependencies.append(_dependency(contract))
    elif selected.nprocs > 1 or runtime['qualification'] == 'recorded':
        raise Unassessed('Parallel or recorded ORCA requires a target-local runtime/MPI contract with helper and library byte identities')
    return executable, dependencies


def _policy(scientific, required=False):
    value = scientific.get('validation')
    if value is None:
        if required:
            raise Unassessed('FREQ requires an explicit minimum/imaginary-frequency validation policy')
        return FrequencyPolicy()
    if not isinstance(value,dict) or set(value) != {'require_minimum','imaginary_tolerance_cm1'} or type(value['require_minimum']) is not bool:
        raise ValueError('Validation requires explicit require_minimum and imaginary_tolerance_cm1')
    if type(value['imaginary_tolerance_cm1']) not in (int,float):
        raise ValueError('Imaginary-frequency tolerance must be an explicit number')
    return FrequencyPolicy(value['require_minimum'],value['imaginary_tolerance_cm1'])


def _scientific(spec):
    scientific = spec['scientific']
    if not isinstance(scientific,dict):
        raise ValueError('scientific must be an object')
    workflow = scientific.get('workflow',False)
    if type(workflow) is not bool:
        raise ValueError('workflow must be boolean')
    allowed = {'profile','workflow','structure','config'} if workflow else {'profile','workflow','input','validation'}
    if set(scientific) - allowed:
        raise Unsupported('Unsupported scientific fields: ' + ', '.join(sorted(set(scientific)-allowed)))
    profile = scientific.get('profile')
    if workflow:
        from cmw.molecular.workflows.opt_freq_sp import build_state, load_workflow_config
        if profile not in {'opt','opt+sp','opt+freq','opt+freq+sp'}:
            raise Unsupported('Unsupported ORCA workflow profile')
        structure = _path(scientific.get('structure'),'structure')
        config_path = _path(scientific.get('config'),'config')
        raw_config = json.loads(_read(config_path))
        if not {'charge','multiplicity','resources','validation','stages'} <= set(raw_config):
            raise ValueError('Workflow config requires explicit state, resources, validation and stage settings')
        if type(raw_config['charge']) is not int or type(raw_config['multiplicity']) is not int:
            raise ValueError('Workflow charge and multiplicity must be explicit integers')
        if not isinstance(raw_config['resources'], dict) or set(raw_config['resources']) != {'nprocs','maxcore_mb_per_process'}:
            raise ValueError('Workflow must explicitly declare nprocs and MaxCore')
        _policy({'validation':raw_config['validation']},required=True)
        if any(type(value) is not int or value < 1 for value in raw_config['resources'].values()):
            raise ValueError('Workflow nprocs and MaxCore must be positive integers')
        config = load_workflow_config(config_path,env={})
        geometry = read_xyz(structure)
        _electronic_state(geometry,config.charge,config.multiplicity)
        for stage in profile.split('+'):
            stage_spec = config.stages[StageType(stage.upper())]
            _keywords(stage_spec.keywords)
            _blocks(stage_spec.blocks)
        _in_domain('resources',_resources,spec['resources'],config.resources)
        executable,deps = _in_domain('runtime',_runtime,spec['runtime'],config.resources)
        state = build_state(mode=profile,structure_path=structure,config_path=config_path,
                           output_override=Path(spec['workdir']),env={},orca_exe=str(executable))
        return {'workflow':True,'config':config,'state':state,'resources':config.resources,
                'dependencies':[structure,config_path],'runtime_dependencies':deps,
                'executable':executable,'policy':config.validation}
    if profile not in {'sp','opt','freq'}:
        raise Unsupported('Supported single ORCA stages are sp, opt and freq')
    parsed = parse_input(_path(scientific.get('input'),'scientific input'),StageType(profile.upper()))
    _in_domain('resources',_resources,spec['resources'],parsed['resources'])
    executable,deps = _in_domain('runtime',_runtime,spec['runtime'],parsed['resources'])
    parsed.update(workflow=False,executable=executable,runtime_dependencies=deps,
                  policy=_policy(scientific,required=profile=='freq'))
    return parsed


def _plan(spec, parsed):
    from cmw.jobs.runner_assets import runner_path, runner_dependencies
    work = _path(spec['workdir'],'workdir')
    if work.exists() and not work.is_dir():
        raise ValueError('Workdir is not a directory')
    dependencies = [_dependency(path) for path in parsed['dependencies']] + parsed['runtime_dependencies']
    selected = parsed['resources']
    policy = parsed['policy']
    env = {'ORCA_EXE':str(parsed['executable']),'PYTHON_BIN':sys.executable,
           'CMW_ORCA_RUNNER':str(runner_path('orca/run_orca.sh')),
           'CMW_ORCA_RUNTIME_CONTRACT':'',
           'CMW_EXTERNAL_SCRATCH_ROOT':'','CMW_EXTERNAL_SCRATCH_MOUNT':'',
           'CMW_EXTERNAL_SCRATCH_MINIMUM_FREE_GIB':'','CMW_EXTERNAL_SCRATCH_REQUIRED_FILESYSTEM':'',
           'OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1',
           'NPROCS':str(selected.nprocs),'MAXCORE_MB':str(selected.maxcore_mb_per_process),
           'CMW_REQUIRE_MINIMUM':'1' if policy.require_minimum else '0',
           'CMW_IMAGINARY_TOLERANCE_CM1':str(policy.imaginary_tolerance_cm1)}
    if spec['runtime'].get('contract'):
        env['CMW_ORCA_RUNTIME_CONTRACT'] = str(_path(spec['runtime']['contract'],'runtime contract'))
    if parsed['workflow']:
        relative = 'workflows/run_opt_freq_sp.sh'
        state = parsed['state']
        state_path = work / 'workflows' / state['workflow_target_id'] / 'workflow_state.json'
        argv = [str(runner_path(relative)),'--state',str(state_path)]
        sources = []
        association = {'workflow_target_id':state['workflow_target_id'],'workflow_state':str(state_path)}
    else:
        relative = 'orca/run_orca.sh'
        paths = {name:work / value for name,value in {'input':'stage.inp','target':'target.json',
            'metadata':'job.json','output':'stage.out','stderr':'stage.err','geometry-contract':'geometry-input.json'}.items()}
        if paths['output'].exists() or paths['metadata'].exists() or paths['stderr'].exists():
            raise ValueError('Scientific outputs already exist; a new no-clobber attempt directory is required')
        argv = [str(runner_path(relative))]
        for name,path in paths.items():
            argv.extend(['--'+name,str(path)])
        if parsed['spec'].stage_type is StageType.OPT:
            argv.extend(['--artifact','final_geometry='+str(work/'stage.xyz')])
        if spec['runtime'].get('contract'):
            argv.extend(['--runtime-contract',env['CMW_ORCA_RUNTIME_CONTRACT'],'--require-runtime-contract'])
        sources = [{'role':'orca_output','path':str(work/'stage.out'),'stage':parsed['spec'].stage_type.value,'optional':False}]
        association = {'stage':parsed['spec'].stage_type.value}
    dependencies.extend(runner_dependencies(relative))
    return {'argv':argv,'env':env,'cwd':str(work),'progress_sources':sources,
            'dependencies':list({item['path']:item for item in dependencies}.values()),
            'scientific_association':association,'runtime_qualification':spec['runtime']['qualification'],
            'result_path':str(Path(parsed['state']['result_path'])) if parsed['workflow'] else str(work/'job.json'),
            'attempt_directory':str(work),
            'suspension':{'eligible':False,'reason':'Reviewed Jobs route is not enrolled for controlled suspension'},
            'memory_semantics':'MaxCore MB per process; total commitment includes 20% headroom and is not an RSS limit'}


def check(spec):
    """Pure check: no engines, subprocess probes, writes, registration or staging."""
    domain='input'
    try:
        parsed = _scientific(spec)
        domain='output_layout'
        plan = _plan(spec,parsed)
        findings=[{'domain':name,'status':'READY','message':message} for name,message in (
            ('input','Supported bounded ORCA dialect and explicit scientific intent'),
            ('dependencies','Exact input and runtime dependencies identified'),
            ('source_provenance','Original input identities retained; no scientific acceptance asserted'),
            ('runtime','Recorded runtime evidence revalidated without executing a probe; '+spec['runtime']['qualification']),
            ('resources',plan['memory_semantics']),('output_layout','New attempt paths; no existing result overwrite'),
            ('monitoring','Exact stage output or explicit workflow state; ORCA 6.1.x bounded native grammar'),
            ('execution','Existing foreground ORCA runner and finalizer; held registration only'))]
        return {'status':'READY','findings':findings,'plan':plan}
    except (OSError,KeyError,TypeError,ValueError) as exc:
        status='UNSUPPORTED' if isinstance(exc,Unsupported) else 'UNASSESSED' if isinstance(exc,Unassessed) else 'ERROR'
        return {'status':status,'findings':[{'domain':getattr(exc,'preflight_domain',domain),'status':status,'message':str(exc)}],'plan':{}}


def prepare(spec, checked):
    """Materialize a new attempt intentionally after a successful pure check."""
    if checked.get('status') != 'READY':
        raise ValueError('Successful preflight is required before preparation')
    work = _path(spec['workdir'],'workdir')
    if not work.is_dir() or any(work.iterdir()):
        raise ValueError('Preparation requires a new empty task-owned workdir')
    for item in checked['plan']['dependencies']:
        if _dependency(Path(item['path']))['sha256'] != item['sha256']:
            raise ValueError('Reviewed dependency changed before preparation: '+item['path'])
    parsed = _scientific(spec)
    plan = _plan(spec,parsed)
    if parsed['workflow']:
        from cmw.molecular.workflows.opt_freq_sp import initialize_state,next_action
        state_path = Path(plan['scientific_association']['workflow_state'])
        initialize_state(state_path,parsed['state'])
        action,record=next_action(state_path)
        if action != 'RUN':
            raise ValueError('New workflow did not yield its first supported stage')
        plan['scientific_association'].update(first_target_id=record['target_id'],first_attempt_id=record['attempt_id'])
        # The state is mutable after execution begins; its initial bytes and the
        # first materialized inputs are pinned only at final launch admission.
        pinned = [state_path, *(Path(record[key]) for key in ('input','target','layout','geometry_contract'))]
        pinned.append(Path(record['input']).parent/'input.xyz')
        plan['dependencies'].extend(_dependency(path) for path in pinned)
        return plan
    source = work / 'source.xyz'
    write_xyz(source,parsed['geometry'])
    artifact=structure_artifact_from_file(source,source='jobs_imported_orca',charge=parsed['charge'],multiplicity=parsed['multiplicity'],
                                         provenance={'imported_input':str(parsed['dependencies'][0]),'sha256':file_hash(parsed['dependencies'][0])})
    geometry_input=prepare_orca_geometry_input(artifact,work/'input.xyz')
    target=make_target_from_geometry_input(geometry_input,spec=parsed['spec'],validation_policy={
        'require_minimum':parsed['policy'].require_minimum,'imaginary_tolerance_cm1':parsed['policy'].imaginary_tolerance_cm1})
    write_target(work/'target.json',target,GeometryLineage(source='jobs_imported_orca',geometry_sha256=target.geometry_sha256))
    atomic_write_json(work/'geometry-input.json',geometry_input.to_dict())
    (work/'stage.inp').write_text(render_orca_input(geometry_input=geometry_input,charge=parsed['charge'],multiplicity=parsed['multiplicity'],
        spec=parsed['spec'],resources=parsed['resources']),encoding='utf-8')
    plan['dependencies'].extend(_dependency(work/name) for name in ('source.xyz','input.xyz','target.json','geometry-input.json','stage.inp'))
    plan['scientific_association'].update(target_id=target.target_id,geometry_sha256=geometry_hash(parsed['geometry']))
    return plan
