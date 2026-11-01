"""Read-only adapters to the existing scientific result validators.

Only explicit review work calls these adapters. They never finalize, normalize
outputs, register jobs, or launch an engine. The caller owns process deadlines,
observer cancellation and exact Jobs/node identity checks.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import hashlib
import os
import stat

from cmw.core.provenance import stable_hash
from cmw.periodic.vasp.result_sources import read_json_source


_DOMAINS = ('source_binding', 'electronic', 'ionic_analysis', 'endpoint', 'policy', 'finalization')


def _domain(status, summary, evidence=None):
    return {'status': status, 'summary': summary, 'evidence': evidence}


def _status(values):
    values = tuple(values)
    return next((s for s in ('FAIL', 'UNSUPPORTED', 'UNKNOWN', 'NOT ASSESSED') if s in values),
                'PASS' if values else 'UNKNOWN')


def _stat(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


class ReviewLimit(ValueError):
    """The requested review cannot fully cover a declared source."""


class UnsupportedReview(ValueError):
    """No supported read-only review contract covers the selected evidence."""


class _Sources:
    def __init__(self, max_bytes, check_cancelled):
        if type(max_bytes) is not int or not 0 < max_bytes <= 1024**3:
            raise ValueError('Review max_bytes must be positive and at most 1 GiB')
        self.max_bytes = max_bytes
        self.check_cancelled = check_cancelled or (lambda: None)
        self.records = {}

    def observe(self, value, role='review_source', *, optional=False):
        self.check_cancelled()
        path = Path(value)
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError('Review sources require their original canonical absolute path')
        key = str(path)
        scientific_input = role.startswith('source:') or role in {'source_result', 'orca_target',
            'prepared_scientific_input', 'artifact:input', 'artifact:input_geometry',
            'artifact:source_structure', 'artifact:geometry_contract'}
        if key in self.records:
            self.records[key]['scientific_input'] |= scientific_input
            return self.records[key]
        if len(self.records) >= 512:
            raise ReviewLimit('Review source closure exceeds 512 files')
        if optional and not path.exists():
            return None
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f'Review source is not a regular file: {path}')
        if before.st_size > self.max_bytes:
            raise ReviewLimit(f'Source exceeds the {self.max_bytes}-byte review limit: {path}')
        digest, count = hashlib.sha256(), 0
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if _stat(os.fstat(stream.fileno())) != _stat(before):
                raise ValueError(f'Review source changed while opening: {path}')
            while chunk := stream.read(min(1024**2, self.max_bytes - count + 1)):
                self.check_cancelled()
                count += len(chunk)
                if count > self.max_bytes:
                    raise ReviewLimit(f'Source grew past review byte limit: {path}')
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if _stat(before) != _stat(after) or _stat(after) != _stat(path.stat()):
            raise ValueError(f'Review source changed while reading: {path}')
        record = {'role': role, 'path': key, 'absolute_path': key, 'resolved_path': key,
                  'stat_before': _stat(before), 'stat_after': _stat(after),
                  'path_stat_after': _stat(after), 'sha256': digest.hexdigest(),
                  'bytes_read': count, 'coverage': 'complete', 'stable': True, 'eof_reached': True,
                  'observed_at': datetime.now(timezone.utc).isoformat(), 'error': None,
                  'scientific_input': scientific_input}
        self.records[key] = record
        return record

    def json(self, value, role='review_metadata'):
        self.observe(value, role)
        result, source = read_json_source(value, role)
        if source['sha256'] != self.records[str(value)]['sha256']:
            raise ValueError('Review metadata changed between identity and parsing')
        return result

    def verify_stable(self):
        for path, source in self.records.items():
            self.check_cancelled()
            if (Path(path).resolve() != Path(path) or _stat(Path(path).stat()) != source['stat_after']
                    or any(Path(alias).resolve() != Path(path) for alias in source.get('declared_aliases', ()))):
                raise ValueError(f'Review source changed during validation: {path}')


def _sources_from(value):
    if not isinstance(value, dict):
        return []
    return [*value.get('sources', ()), *([value['policy_source']] if value.get('policy_source') else ()),
            *_sources_from(value.get('binding'))]


def _vasp(job, directory, policy_path, spec_path, segment, stdout, sources):
    from cmw.periodic.vasp.result_finalization import inspect_with_policy
    root = Path(directory)
    # The existing native parser retains its exact partial-read evidence. Other
    # binding readers are full-file validators, so bound their known closure first.
    for name in ('POSCAR', 'CONTCAR', 'INCAR'):
        if (root / name).exists() and (root / name).stat().st_size > sources.max_bytes:
            raise ReviewLimit(f'Source exceeds the {sources.max_bytes}-byte review limit: {root / name}')
    if policy_path:
        sources.observe(policy_path, 'policy')
    if spec_path:
        spec = sources.json(spec_path, 'finalization_spec')
        execution = spec.get('execution', {})
        for key in ('snapshot', 'receipt'):
            if execution.get(key):
                path = Path(execution[key])
                sources.observe(path if path.is_absolute() else Path(spec_path).parent / path, 'jobs_' + key)
        runner = execution.get('runner_record')
        if runner:
            path = Path(runner['path'])
            sources.observe(path if path.is_absolute() else Path(spec_path).parent / path, 'runner_record')
            for name in ('INCAR', 'POSCAR', 'POTCAR', 'KPOINTS'):
                sources.observe(root / name, 'runner_input:' + name)
        if spec.get('preparation_record'):
            path = Path(spec['preparation_record']['path'])
            sources.observe(path if path.is_absolute() else Path(spec_path).parent / path, 'preparation_record')
    evidence = inspect_with_policy(root, policy_path=policy_path, spec_path=spec_path,
        segment=segment, stdout=stdout, max_bytes=sources.max_bytes)
    coverage = evidence.get('coverage', {})
    complete = coverage.get('all_sources_complete') and coverage.get('all_sources_stable')
    mode = evidence.get('mode', {})
    current = evidence.get('selected_segment') or {}
    evaluations = current.get('evaluations', [])
    states = [('FAIL' if e.get('native_convergence') is False or e.get('numeric_convergence') == 'FAIL'
               else 'PASS' if e.get('native_convergence') is True and e.get('numeric_convergence') == 'PASS'
               else 'UNKNOWN') for e in evaluations]
    electronic = _status(states)
    if not mode.get('supported'):
        electronic = 'UNSUPPORTED' if evidence.get('status') == 'unsupported' else 'UNKNOWN'
    elif not complete or not coverage.get('full_electronic_history'):
        electronic = 'UNKNOWN' if electronic == 'PASS' else electronic
    binding = evidence.get('binding')
    assessment = evidence.get('policy_assessment')
    checks = (assessment or {}).get('checks', [])
    endpoint = evidence.get('endpoint', {})
    ionic = evidence.get('ionic', {})
    ionic_checks = [c for c in checks if c['code'].startswith(('ionic.', 'forces.', 'constraints.'))]
    ionic_status = ('NOT APPLICABLE' if mode.get('calculation') == 'static' else
                    _status(c['status'] for c in ionic_checks) if ionic_checks else
                    'UNSUPPORTED' if evidence.get('status') == 'unsupported' else 'NOT ASSESSED')
    endpoint_checks = [c for c in checks if c['code'].startswith(('endpoint.', 'artifact.'))]
    endpoint_status = _status(c['status'] for c in endpoint_checks) if endpoint_checks else 'NOT ASSESSED'
    domains = {
        'source_binding': _domain('PASS' if binding and binding.get('valid') else 'FAIL' if binding else 'NOT ASSESSED',
            'Explicit retrospective input and execution binding' if binding else 'No explicit input/execution binding selected',
            binding),
        'electronic': _domain(electronic, 'Native and numerical electronic convergence within observed coverage',
            {'evaluation_count': len(evaluations), 'full_history': coverage.get('full_electronic_history'),
             'final_evaluation': {key: evaluations[-1].get(key) for key in
                ('index', 'native_ionic_label', 'native_convergence', 'numeric_convergence', 'effective_ediff')} if evaluations else None}),
        'ionic_analysis': _domain(ionic_status, 'Static calculation: ionic relaxation is not applicable' if ionic_status == 'NOT APPLICABLE'
            else 'Ionic stopping and explicitly scoped force evidence; no unselected convergence policy',
            {'ionic': ionic, 'force_summary': endpoint.get('force_summary'), 'constraints': evidence.get('constraints'), 'checks': ionic_checks}),
        'endpoint': _domain(endpoint_status, 'Endpoint geometry/output checks under selected policy' if assessment else 'Endpoint comparisons observed; no acceptance policy',
            {'comparisons': endpoint.get('comparisons'), 'checks': endpoint_checks}),
        'policy': _domain(assessment['status'] if assessment else 'NOT ASSESSED',
            assessment['interpretation'] if assessment else 'No acceptance policy selected', assessment),
        'finalization': _domain('NOT ASSESSED', 'Review does not finalize or reverify a published artifact record'),
    }
    compact = {k: evidence.get(k) for k in ('kind', 'status', 'parser', 'run_directory', 'snapshot_id', 'mode', 'conflicts', 'error')}
    compact['selected_segment'] = {k: current.get(k) for k in ('index', 'segment_id', 'version', 'termination')}
    return {'domains': domains, 'evidence': compact, 'sources': _sources_from(evidence),
            'policy_identity': (assessment or {}).get('policy_identity'), 'coverage': coverage,
            'limitations': evidence.get('limitations', [])}


def _policy(path, engine, profile, sources):
    if not path:
        return None, None
    value = sources.json(path, 'review_policy')
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'name', 'engine', 'profile', 'frequency'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['engine'] != engine or value['profile'] != profile
            or not isinstance(value['name'], str) or not value['name'].strip() or len(value['name']) > 256):
        raise ValueError('Review policy must explicitly match schema 1, engine and prepared profile')
    frequency = value['frequency']
    if engine == 'ORCA' and 'freq' in profile.lower():
        from cmw.molecular.orca.status import FrequencyPolicy
        if (not isinstance(frequency, dict) or set(frequency) != {'require_minimum', 'imaginary_tolerance_cm1'}
                or type(frequency['require_minimum']) is not bool
                or type(frequency['imaginary_tolerance_cm1']) not in (int, float)):
            raise ValueError('Frequency assessment requires an explicit minimum/imaginary-frequency policy')
        FrequencyPolicy(**frequency)
    elif frequency is not None:
        raise ValueError('Frequency policy is only applicable to an ORCA frequency profile')
    return value, {'schema_version': 1, 'name': value['name'], 'policy_id': stable_hash(value),
                   'path': str(path), 'sha256': sources.records[str(path)]['sha256']}


def _molecular_binding(job, sources):
    from .molecular import validate_binding, launch_blocker
    if not job.get('molecular_submission'):
        raise UnsupportedReview('Molecular review requires an exact first-class prepared attempt association')
    value = validate_binding(job['molecular_submission'])
    plan = value['plan']
    workflow_state = plan.get('scientific_association', {}).get('workflow_state')
    science = value['spec']['scientific']
    declared_inputs = {science[key] for key in ('input', 'structure', 'config', 'source_result',
        'fragments_file', 'configuration_file', 'orca_output') if isinstance(science.get(key), str)}
    for item in plan['dependencies']:
        # Workflow state changes as the prepared workflow advances. Original
        # admission bytes must not be treated as the terminal state identity.
        if item['path'] == workflow_state:
            continue
        dependency = Path(item['path'])
        if (job['engine'] == 'Multiwfn' and plan.get('profile') == 'IFCT'
                and dependency.name == 'cmw-orca-excited-state.out'
                and dependency.parent == Path(plan['attempt_directory'])):
            original = Path(value['spec']['scientific']['orca_output'])
            if not dependency.is_symlink() or dependency.resolve() != original:
                raise ValueError('Prepared IFCT ORCA alias no longer targets the original donor')
            actual = sources.observe(original, 'prepared_scientific_input')
            actual.setdefault('declared_aliases', []).append(str(dependency))
        else:
            actual = sources.observe(dependency, 'prepared_scientific_input' if item['path'] in declared_inputs else 'prepared_dependency')
        if actual['sha256'] != item['sha256']:
            raise ValueError('Prepared dependency changed: ' + item['path'])
    blocker = launch_blocker(job)
    if blocker:
        raise ValueError(blocker)
    return value


def _manifest_sources(path, record, sources):
    from cmw.molecular.workflows.source import _artifact_path
    for role, artifact in record.get('artifacts', {}).items():
        if not isinstance(artifact, dict):
            raise ValueError('Malformed artifact manifest')
        sources.observe(_artifact_path(path, artifact, record), 'artifact:' + role)


def _orca(job, value, policy, sources):
    from cmw.molecular.orca.status import parse_orca_output, validate_stage, StageType, FrequencyPolicy
    from cmw.molecular.orca.job import check_reuse
    plan = value['plan']
    association = plan.get('scientific_association', {})
    rows = []
    if association.get('workflow_state'):
        state = sources.json(association['workflow_state'], 'workflow_state')
        if (state.get('schema_version') != 1 or state.get('workflow') != 'opt_freq_sp'
                or state.get('workflow_target_id') != association.get('workflow_target_id')):
            raise ValueError('ORCA workflow identity differs from the registered association')
        order = state.get('stage_order')
        if not isinstance(order, list) or not 1 <= len(order) <= 3 or len(set(order)) != len(order):
            raise ValueError('Invalid ORCA workflow stage selection')
        for name in order:
            entry = state['stages'][name]
            runner = entry.get('runner')
            if not runner:
                if entry.get('metadata') and entry.get('artifacts', {}).get('output'):
                    from cmw.core.execution_layout import resolve_recorded_layout
                    metadata = sources.json(entry['metadata'], 'orca_finalizer_record')
                    layout = resolve_recorded_layout(metadata['execution_layout'], metadata_path=Path(entry['metadata']))
                    rows.append({'stage': name, 'output': entry['artifacts']['output']['path'],
                        'target': str(layout.target_directory / 'target.json'), 'metadata': entry['metadata'],
                        'target_id': entry['target_id'], 'attempt_id': entry['attempt_id']})
                else:
                    rows.append({'stage': name, 'pending': True})
                continue
            if any(runner.get(k) != entry.get(k) for k in ('target_id', 'attempt_id')) or runner.get('stage') != name:
                raise ValueError('ORCA stage runner association differs from the recorded attempt')
            rows.append({'stage': name, 'output': runner['output'], 'target': runner['target'],
                         'metadata': runner['metadata'], 'target_id': entry['target_id'], 'attempt_id': entry['attempt_id']})
    else:
        root = Path(plan['cwd'])
        rows.append({'stage': association.get('stage'), 'output': str(root / 'stage.out'),
                     'target': str(root / 'target.json'), 'metadata': plan['result_path'],
                     'target_id': association.get('target_id')})
    stages, electronic, analysis, integrity = [], [], [], []
    for row in rows:
        stage = StageType(row['stage'])
        if stage not in {StageType.SP, StageType.OPT, StageType.FREQ}:
            raise ValueError('Unsupported ORCA result-review stage')
        if row.get('pending') or not Path(row['output']).exists():
            stages.append({**row, 'status': 'UNKNOWN', 'reason': 'Exact stage output is pending'})
            electronic.append('UNKNOWN'); analysis.append('UNKNOWN'); integrity.append('UNKNOWN')
            continue
        sources.observe(row['output'], 'orca_output:' + stage.value)
        target = sources.json(row['target'], 'orca_target')['target']
        if target.get('target_id') != row['target_id']:
            raise ValueError('ORCA stage target differs from prepared identity')
        native = parse_orca_output(Path(row['output']).read_text())
        if native.orca_version is not None and not native.orca_version.startswith('6.1.'):
            raise UnsupportedReview('ORCA result review is bounded to the existing 6.1.x native dialect')
        e_status = 'FAIL' if native.scf_failure_evidence or native.fatal_evidence else 'PASS' if native.scf_converged else 'UNKNOWN'
        electronic.append(e_status)
        frequency = (policy or {}).get('frequency')
        assessed = None
        if stage is not StageType.FREQ or frequency is not None:
            assessed = validate_stage(native, stage, frequency_policy=FrequencyPolicy(**frequency) if frequency else FrequencyPolicy())
        status = ('NOT ASSESSED' if assessed is None else
                  {'VALID': 'PASS', 'INVALID': 'FAIL', 'NOT_CONVERGED': 'FAIL'}.get(assessed.status.value, 'UNKNOWN'))
        analysis.append('NOT APPLICABLE' if stage is StageType.SP else status)
        reuse = {'reuse': False, 'code': 'RECORD_PENDING', 'reason': 'Finalizer record is absent'}
        if Path(row['metadata']).exists():
            record = sources.json(row['metadata'], 'orca_finalizer_record')
            if record.get('target', {}).get('target_id') != row['target_id'] or (
                    row.get('attempt_id') and record.get('attempt', {}).get('attempt_id') != row['attempt_id']):
                raise ValueError('ORCA result identity differs from the exact workflow stage')
            _manifest_sources(Path(row['metadata']), record, sources)
            reuse = check_reuse(Path(row['target']), Path(row['metadata']))
        integrity.append('PASS' if reuse['reuse'] else 'UNKNOWN' if reuse['code'] == 'RECORD_PENDING' else 'FAIL')
        stages.append({'stage': stage.value, 'native': native.to_dict(), 'validation': asdict(assessed) if assessed else None,
                       'validation_status': status, 'reverification': reuse})
    return {'source_binding': _domain('PASS', 'Exact registered molecular plan and dependency bytes checked'),
            'electronic': _domain(_status(electronic), 'Observed ORCA SCF evidence remains separate from Jobs outcome', stages),
            'ionic_analysis': _domain('NOT APPLICABLE' if set(analysis) == {'NOT APPLICABLE'} else _status(s for s in analysis if s != 'NOT APPLICABLE'),
                'Stage-specific OPT/FREQ evidence; minimum-character policy must be explicit', stages),
            'endpoint': _domain(_status(integrity), 'Existing ORCA result and required artifact reuse checks', stages),
            'finalization': _domain(_status(integrity), 'Existing records rechecked without publishing or rewriting them', stages)}, stages


def _multiwfn(job, value, sources):
    from cmw.molecular.multiwfn.jobs import verify_prepared
    from cmw.molecular.workflows.source import _artifact_path
    plan = value['plan']
    attempt = Path(plan['attempt_directory'])
    profile = plan['profile']
    if profile not in {'FMO', 'ESP', 'IGMH', 'IFCT'}:
        raise ValueError('Unsupported Multiwfn review profile')
    donor = sources.json(plan['source_result'], 'source_result')
    for role in ('wavefunction', 'geometry'):
        sources.observe(_artifact_path(Path(plan['source_result']), donor['artifacts'][role], donor), 'source:' + role)
    for name in ('runtime.json', 'menu.in', 'multiwfn-runtime-alias.txt', 'multiwfn.log', 'multiwfn.stderr',
                 'health-latest.json', 'health-history.jsonl', 'execution-layout.json', 'command.json', 'analysis-plan.json'):
        sources.observe(attempt / name, name, optional=True)
    for output in plan['expected_outputs']:
        sources.observe(attempt / output['output_path'], 'output:' + output['role'], optional=not output.get('required', True))
    verify_prepared(Path(plan['prepared_path']), terminal=True)
    if job.get('status') != 'Done' or type(job.get('exit_code')) is not int or job['exit_code'] != 0:
        raise ValueError('Task-specific terminal validation requires an independently successful Jobs process outcome')
    if profile == 'IFCT':
        from cmw.molecular.multiwfn.ifct_job import inspect
        result = inspect(attempt, job['exit_code'])
    else:
        from cmw.molecular.workflows.multiwfn_analysis import inspect_analysis
        result = inspect_analysis(target_path=Path(plan['scientific_plan']['target_path']), runtime_path=attempt / 'runtime.json',
            menu_path=attempt / 'menu.in', attempt_directory=attempt, process_exit_code=job['exit_code'])
    record = sources.json(plan['result_path'], 'multiwfn_finalizer_record') if Path(plan['result_path']).exists() else None
    return {'source_binding': _domain('PASS', 'Original target-local donor, prepared menu and runtime bytes rechecked'),
            'electronic': _domain('NOT APPLICABLE', 'Multiwfn analysis has no electronic-convergence claim'),
            'ionic_analysis': _domain('PASS', 'Existing ' + profile + ' task-specific validator applied without finalization',
                {'profile': profile, 'validation_status': result.get('validation_status', result.get('scientific', {}).get('status'))}),
            'endpoint': _domain('PASS', 'Declared output identities, geometry/grid or IFCT source/state checks completed'),
            'finalization': _domain('UNKNOWN' if record else 'NOT ASSESSED',
                'Existing record observed; current validation does not publish or refresh it' if record else 'No finalizer record observed')}, {'profile': profile, 'existing_record': bool(record)}


def inspect_engine(job: dict, *, directory: str | None = None, policy_path: str | None = None,
                   spec_path: str | None = None, segment: int | None = None, stdout: str | None = None,
                   max_bytes: int, check_cancelled: Callable[[], None] | None = None) -> dict:
    """Review one selected owner-local job; no overall scientific verdict exists.

    Byte limits are per declared source. The service must also bound wall time,
    response size and process lifetime, since existing validators are synchronous.
    """
    engine = job.get('engine')
    sources = _Sources(max_bytes, check_cancelled)
    result = {'engine': engine, 'domains': {name: _domain('NOT ASSESSED', 'Not inspected') for name in _DOMAINS},
              'evidence': {}, 'sources': [], 'policy_identity': None,
              'coverage': {'max_bytes_per_source': max_bytes, 'complete': False},
              'limitations': ['Review is observational; it neither finalizes scientific artifacts nor authorizes execution.']}
    try:
        if engine == 'VASP':
            if not directory:
                raise ValueError('VASP review requires an exact declared scientific output directory')
            result.update(_vasp(job, directory, policy_path, spec_path, segment, stdout, sources))
        elif engine in {'ORCA', 'Multiwfn'}:
            if spec_path is not None or segment is not None or stdout is not None:
                raise ValueError('VASP binding/segment/stdout options do not apply to molecular review')
            value = _molecular_binding(job, sources)
            profile = value['spec']['scientific']['profile']
            policy, identity = _policy(policy_path, engine, profile, sources)
            domains, evidence = (_orca(job, value, policy, sources) if engine == 'ORCA' else _multiwfn(job, value, sources))
            status = _status(d['status'] for key, d in domains.items() if key != 'finalization' and d['status'] != 'NOT APPLICABLE')
            if policy and status == 'PASS' and (job.get('status') != 'Done' or type(job.get('exit_code')) is not int
                    or job['exit_code'] != 0 or job.get('signal') is not None or job.get('cancel_requested') is True):
                status = 'UNKNOWN'
            domains['policy'] = _domain(status if policy else 'NOT ASSESSED',
                'Assessment under the exact selected task-specific policy' if policy else 'No acceptance policy selected',
                {'identity': identity, 'checks': {name: d['status'] for name, d in domains.items()}} if policy else None)
            result.update(domains=domains, evidence=evidence, policy_identity=identity,
                          coverage={'max_bytes_per_source': max_bytes, 'complete': True})
        else:
            result['domains'] = {name: _domain('UNSUPPORTED', 'No result-review adapter for this engine') for name in _DOMAINS}
        sources.verify_stable()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        status = ('UNSUPPORTED' if isinstance(exc, UnsupportedReview) else
                  'UNKNOWN' if isinstance(exc, (ReviewLimit, FileNotFoundError)) else 'FAIL')
        result['domains'] = {name: _domain(status, str(exc)) for name in _DOMAINS}
        if not policy_path:
            result['domains']['policy'] = _domain('NOT ASSESSED', 'No acceptance policy selected')
        result['domains']['finalization'] = _domain('NOT ASSESSED', 'No finalization performed')
        result['coverage'] = {'max_bytes_per_source': max_bytes, 'complete': False, 'reason': str(exc)}
    known = {(s.get('resolved_path'), s.get('sha256')) for s in result['sources']}
    result['sources'].extend(record for record in sources.records.values() if (record['resolved_path'], record['sha256']) not in known)
    return result
