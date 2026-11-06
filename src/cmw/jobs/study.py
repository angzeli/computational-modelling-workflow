"""Explicit owner-local study orchestration; preparation is never dispatch."""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from uuid import UUID, uuid5

from cmw.core.provenance import stable_hash
from .catalogue import CatalogueStore
from .lifecycle import read_job
from .locations import read_location_metadata
from .runner_assets import file_identity
from .store import JobsError, Store
from .successor import _closure, _path
from .study_models import StudyResult

_LIMIT = 4 * 1024 * 1024
_INPUTS = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')


def _read(path):
    path = _path(str(path))
    before = file_identity(path, max_bytes=_LIMIT)
    value = read_location_metadata(path, max_bytes=_LIMIT)
    if before != file_identity(path, max_bytes=_LIMIT) or type(value) is not dict:
        raise JobsError('Study record changed or is not a JSON object')
    return value


def _write(path, value):
    raw = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > _LIMIT:
        raise JobsError('Study record exceeds 4 MiB')
    fd = os.open(_path(str(path)), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())


def _request(study_id, case_id, purpose='registration'):
    return uuid5(UUID(hex=study_id), purpose + ':' + case_id).hex


def _case(plan, case_id):
    matches = [case for case in plan['preparation']['cases'] if case['case_id'] == case_id]
    if len(matches) != 1:
        raise JobsError('Unknown or ambiguous study case identity')
    return matches[0]


def _prepared(case, plan):
    for name in _INPUTS:
        path = _path(str(Path(case['output']) / name))
        if file_identity(path, max_bytes=64 * 1024 * 1024)['sha256'] != case['prepared_inputs'][name]['sha256']:
            raise JobsError('Prepared study input changed: ' + name)
    from cmw.core.preparation_publication import inspect_publication
    record_path = _path(str(Path(case['record_directory']) / 'preparation.json'))
    record_identity = file_identity(record_path, max_bytes=_LIMIT)
    record = _read(record_path)
    publication = record.get('publication', {})
    binding = record.get('study', {})
    if (publication.get('state') != 'complete' or publication.get('input_path') != case['output']
            or publication.get('record_path') != str(record_path)
            or any(record.get(key) != value for key, value in case['preparation'].items())
            or binding.get('study_id') != plan['study_id'] or binding.get('case_id') != case['case_id']
            or binding.get('planner_digest') != plan['preparation']['digest']
            or inspect_publication(record_path, input_path=case['output'])['status'] != 'valid'
            or file_identity(record_path, max_bytes=_LIMIT) != record_identity):
        raise JobsError('Original completed study preparation record is missing, changed or incomplete')
    from cmw.periodic.vasp.inputs import check_inputs
    if check_inputs(case['output'])['status'] != 'valid':
        raise JobsError('Prepared study four-input validation failed')


class StudyService:
    def __init__(self, store, origin=None):
        self.store, self.origin = store, dict(origin or {})

    def _owner(self):
        owner = self.store.queue_identity()
        if not owner:
            raise JobsError('Studies require an already enrolled owner queue; no automatic enrollment')
        if any(key in self.origin and self.origin[key] != owner[key] for key in ('node_id', 'queue_id', 'host_id')):
            raise JobsError('Study owner identity changed')
        return owner

    def _result(self, plan, operation, status, payload):
        return StudyResult(1, plan['node_id'], plan['queue_id'], plan['study_id'], operation,
                           status, stable_hash(plan), json.dumps(payload, sort_keys=True, allow_nan=False))

    def _build(self, manifest_path):
        from .study_plan import plan_study
        from .study_comparison import validate_comparison_spec
        from cmw.nodes.identity import load_node, check_node
        from cmw.nodes.submission import _argv
        try:
            prepared = plan_study(manifest_path)
            spec = prepared['spec']
            validate_comparison_spec(spec)
            if spec['comparison_rule']['reference_case'] not in {c['case_id'] for c in prepared['cases']}:
                raise JobsError('Study reference must select one declared case identity')
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise JobsError('Malformed study manifest') from exc
        owner = self._owner()
        execution = spec['execution']
        keys = {'node_id', 'queue_id', 'node_config', 'profile', 'resources', 'vasp', 'run_root'}
        if type(execution) is not dict or set(execution) != keys:
            raise JobsError('Study execution requires explicit owner, runtime profile, resources and run_root')
        if any(execution[key] != owner[key] for key in ('node_id', 'queue_id')):
            raise JobsError('Study belongs to another owner queue; cross-host adoption is unsupported')
        config = load_node(_path(execution['node_config']))
        if (config['node_id'] != owner['node_id'] or not check_node(config)['ready']
                or not any(q['queue_id'] == owner['queue_id'] and q['state'] == str(self.store.root)
                           for q in config['queues'].values())):
            raise JobsError('Study runtime configuration is not ready on the exact original queue')
        if type(execution['profile']) is not str or type(execution['vasp']) is not dict:
            raise JobsError('Study profile and VASP runtime settings are malformed')
        profile = config['profiles'].get(execution['profile'])
        if not profile or profile['engine'] != 'VASP':
            raise JobsError('Study requires an enrolled VASP runtime profile')
        resources = execution['resources']
        if type(resources) is not dict or set(resources) != {'cpus', 'memory_gib', 'mpi_ranks', 'threads_per_rank'}:
            raise JobsError('Study resources must be explicitly declared separately from scientific values')
        for key, value in resources.items():
            if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    or (key != 'memory_gib' and type(value) is not int)):
                raise JobsError('Study resources must be positive finite numbers')
        if (resources['threads_per_rank'] != 1 or resources['cpus'] < resources['mpi_ranks']
                or resources['cpus'] > profile['max_cpus'] or resources['mpi_ranks'] > profile['max_ranks']):
            raise JobsError('Study resources exceed the supported one-thread-per-rank runtime profile')
        run_root = _path(execution['run_root'])
        if not run_root.is_dir():
            raise JobsError('Explicit run_root must already exist')
        runtimes = {}
        for case in prepared['cases']:
            declared = case['preparation']['expected_runtime']
            if (any(declared.get(k) != resources[k] for k in ('cpus', 'mpi_ranks', 'threads_per_rank'))
                    or any(declared.get('incar', {}).get(tag) != execution['vasp'].get(option)
                           for tag, option in (('NCORE', 'ncore'), ('KPAR', 'kpar')))):
                raise JobsError('Study preparation resources/overlay differ from explicit runtime settings')
            run = run_root / case['case_id']
            protected = [self.store.root.resolve(), Path(spec['baseline']['directory']), Path(case['output'])]
            if run.exists() or any(run == p or run.is_relative_to(p) or p.is_relative_to(run) for p in protected):
                raise JobsError('Study execution directory must be new and outside scientific inputs and Jobs state')
            argv = _argv({'engine': 'VASP', 'vasp': execution['vasp'], 'resources': resources},
                         profile, Path(case['output']).parent, run, run / 'native', input_directory=Path(case['output']))
            runtimes[case['case_id']] = {'argv': argv, 'cwd': case['output'], 'run_directory': str(run),
                                       'env': {'CMW_JOBS_OWN_SESSION': '1', 'OMP_NUM_THREADS': '1'}}
        plan = {'schema': 1, 'kind': 'cmw-study-plan', 'study_id': spec['study_id'],
                'node_id': owner['node_id'], 'queue_id': owner['queue_id'], 'state_directory': str(self.store.root),
                'manifest': spec, 'preparation': prepared, 'runtime_id': config['runtime']['fingerprint'],
                'profile_record': profile, 'runtimes': runtimes}
        # Establish the complete bounded review response before any publication.
        self._result(plan, 'plan', 'READY' if prepared['status'] == 'READY' else 'BLOCKED', plan)
        return plan

    def plan(self, manifest_path):
        plan = self._build(manifest_path)
        return self._result(plan, 'plan', 'READY' if plan['preparation']['status'] == 'READY' else 'BLOCKED', plan)

    def materialize(self, manifest_path, approved_digest):
        from .study_plan import materialize_study
        plan = self._build(manifest_path)
        if plan['preparation']['status'] != 'READY':
            raise JobsError('Unassessed study inputs cannot be materialized')
        if stable_hash(plan) != approved_digest:
            raise JobsError('Study plan changed; review its exact new digest before materializing')
        path = Path(plan['manifest']['record_root']) / 'study.json'
        if path.exists():
            raise JobsError('Study materialization record already exists; no-clobber publication')
        published = materialize_study(manifest_path, plan['preparation']['digest'])
        cases = []
        for case in plan['preparation']['cases']:
            row = {'case_id': case['case_id'], 'status': 'INCOMPLETE'}
            try:
                publication = next((r for r in published['cases'] if r['case_id'] == case['case_id']), None)
                if publication is None or publication['status'] != 'MATERIALIZED':
                    raise JobsError('Case publication is incomplete; existing bytes do not establish publication')
                _prepared(case, plan)
                runtime = plan['runtimes'][case['case_id']]
                run = Path(runtime['run_directory'])
                run.mkdir(mode=0o700)
                info = Path(runtime['cwd']).stat()
                parent = run.stat()
                row.update(status='MATERIALIZED', working_identity={'device': info.st_dev, 'inode': info.st_ino},
                           run_identity={'device': parent.st_dev, 'inode': parent.st_ino})
            except (OSError, ValueError) as exc:
                row['reason'] = str(exc)[:2048]
            cases.append(row)
        status = 'MATERIALIZED' if all(row['status'] == 'MATERIALIZED' for row in cases) else 'PARTIAL'
        record = {'schema': 1, 'kind': 'cmw-study-materialization', 'record_path': str(path),
                  'plan_digest': approved_digest, 'status': status, 'plan': plan, 'publication': published, 'cases': cases}
        _write(path, record)
        return self._result(plan, 'materialize', status, {'record_path': str(path), 'cases': cases,
                            'message': 'Prepared only; no Jobs registration or execution requested'})

    def _record(self, record_path, approved_digest=None):
        value = _read(record_path)
        try:
            plan = value['plan']; digest = stable_hash(plan)
            cases = plan['preparation']['cases']
            if (type(cases) is not list or not 1 <= len(cases) <= 64
                    or any(type(c) is not dict or type(c.get('case_id')) is not str
                           or re.fullmatch('[a-f0-9]{64}', c['case_id']) is None for c in cases)
                    or len({c['case_id'] for c in cases}) != len(cases)
                    or plan['preparation']['case_count'] != len(cases)
                    or plan['preparation']['status'] != 'READY'):
                raise JobsError('Study record must retain 1..64 unique ready cases')
            expected = Path(plan['manifest']['record_root']) / 'study.json'
            if (value['kind'] != 'cmw-study-materialization' or value['schema'] != 1
                    or value['record_path'] != str(_path(record_path)) or expected != _path(record_path)
                    or digest != value['plan_digest'] or (approved_digest is not None and digest != approved_digest)
                    or value['status'] not in {'MATERIALIZED', 'PARTIAL'}):
                raise JobsError('Study materialization record or exact approved plan is invalid')
            owner = self._owner()
            if (any(plan[key] != owner[key] for key in ('node_id', 'queue_id'))
                    or plan['state_directory'] != str(self.store.root)):
                raise JobsError('Study record belongs to another original owner queue')
            return value, plan
        except (KeyError, TypeError) as exc:
            raise JobsError('Malformed study materialization record') from exc

    def _runtime(self, plan):
        from cmw.nodes.identity import load_node, check_node
        execution = plan['manifest']['execution']
        config = load_node(execution['node_config'])
        if (not check_node(config)['ready'] or config['runtime']['fingerprint'] != plan['runtime_id']
                or config['node_id'] != plan['node_id'] or config['profiles'].get(execution['profile']) != plan['profile_record']
                or not any(q['queue_id'] == plan['queue_id'] and q['state'] == str(self.store.root)
                           for q in config['queues'].values())):
            raise JobsError('Original study runtime, profile or queue changed')

    def _lookup(self, plan, case):
        request_id = _request(plan['study_id'], case['case_id'])
        previous = self.store.request_lookup(request_id)
        digest = stable_hash({'study_plan': stable_hash(plan), 'case_id': case['case_id']})
        if previous and (previous['operation'] != 'submit' or previous['digest'] != digest):
            raise JobsError('Study case request ID is already bound to a different plan')
        return request_id, digest, previous

    def register(self, record_path, approved_digest, request_id):
        record, plan = self._record(record_path, approved_digest)
        if request_id != plan['study_id']:
            raise JobsError('Use the manifest study_id as the stable registration request ID')
        self._runtime(plan)
        rows = []
        for case in plan['preparation']['cases']:
            row = {'case_id': case['case_id'], 'status': 'UNREGISTERED'}
            try:
                request, digest, previous = self._lookup(plan, case)
                if previous:
                    original = previous['result']
                    job, _ = read_job(self.store, original['id'])
                    if job['attempt_id'] != original['attempt_id']:
                        raise JobsError('Registered study attempt changed; no replacement selected')
                else:
                    publication = next(r for r in record['cases'] if r['case_id'] == case['case_id'])
                    if publication['status'] != 'MATERIALIZED':
                        raise JobsError('Case publication is incomplete')
                    _prepared(case, plan)
                    runtime = plan['runtimes'][case['case_id']]
                    work = Path(runtime['cwd']).stat()
                    observed = {'device': work.st_dev, 'inode': work.st_ino}
                    parent = _path(runtime['run_directory']).stat()
                    run_identity = {'device': parent.st_dev, 'inode': parent.st_ino}
                    if observed != publication['working_identity'] or run_identity != publication['run_identity']:
                        raise JobsError('Original study input or execution directory changed')
                    metadata = {'schema': 1, 'node_id': plan['node_id'], 'queue_id': plan['queue_id'],
                        'request_id': request, 'digest': digest, 'runtime_id': plan['runtime_id'],
                        'study': {'study_id': plan['study_id'], 'case_id': case['case_id'], 'plan_digest': approved_digest,
                                  'record_path': record_path, 'working_identity': observed, 'run_identity': run_identity}}
                    job = self.store.add(argv=runtime['argv'], cwd=runtime['cwd'], env=runtime['env'],
                        name=case['name'], engine='VASP', **plan['manifest']['execution']['resources'], hold=True,
                        remote_request={'request_id': request, 'digest': digest}, node_submission=metadata)
                target = {key: plan[key] for key in ('node_id', 'queue_id')}
                target.update(job_id=job['id'], attempt_id=job['attempt_id'])
                row.update(status='REGISTERED', target=target, replayed=previous is not None,
                           request_id=request, process=job['status'])
                receipt = Path(plan['manifest']['record_root']) / ('registration-' + case['case_id'] + '.json')
                if receipt.exists():
                    prior = _read(receipt)
                    if prior['target'] != target or prior['plan_digest'] != approved_digest:
                        raise JobsError('Original case registration receipt changed')
                else:
                    _write(receipt, {'schema': 1, 'plan_digest': approved_digest, 'case_id': case['case_id'],
                                     'request_id': request, 'target': target})
                catalogue = CatalogueStore(self.store)
                # Retried annotations use their original revision and durable request.
                catalogue.edit(job['id'], {'series': plan['manifest']['name'], 'research_stage': 'convergence-study'},
                    expected_revision=0, expected_attempt_id=job['attempt_id'],
                    request_id=_request(plan['study_id'], case['case_id'], 'catalogue'))
            except (OSError, ValueError, KeyError, StopIteration) as exc:
                row.update(status='INCOMPLETE', reason=str(exc)[:2048])
            rows.append(row)
        status = 'REGISTERED' if all(r['status'] == 'REGISTERED' for r in rows) else 'PARTIAL'
        return self._result(plan, 'register', status, {'record_path': record_path, 'cases': rows,
                            'message': 'Held registration only; no atomic cross-node transaction or dispatch'})

    def view(self, record_path):
        _, plan = self._record(record_path)
        rows = []
        for case in plan['preparation']['cases']:
            row = {'case_id': case['case_id'], 'name': case['name'], 'status': 'NOT_REGISTERED', 'variables': case['variables']}
            try:
                _, _, prior = self._lookup(plan, case)
                if prior:
                    old = prior['result']; job, _ = read_job(self.store, old['id'])
                    if job['attempt_id'] != old['attempt_id']:
                        raise JobsError('Registered attempt changed; no replacement was selected')
                    row.update(status='REGISTERED', process=job['status'], target={
                        'node_id': plan['node_id'], 'queue_id': plan['queue_id'],
                        'job_id': job['id'], 'attempt_id': job['attempt_id']}, binding='Exact recorded study case')
            except (OSError, ValueError) as exc:
                row.update(status='UNAVAILABLE', reason=str(exc)[:2048])
            rows.append(row)
        return self._result(plan, 'view', 'COMPLETE' if all(r['status'] == 'REGISTERED' for r in rows) else 'PARTIAL',
                            {'cases': rows, 'metric': plan['manifest']['metric'],
                             'message': 'Metadata only; explicit comparison selects scientific evidence'})

    def view_for_job(self, job_id, expected_attempt_id):
        job, _ = read_job(self.store, job_id)
        owner = self._owner()
        target = {key: owner[key] for key in ('node_id', 'queue_id')}
        target.update(job_id=job['id'], attempt_id=job['attempt_id'])
        if expected_attempt_id != job['attempt_id']:
            raise JobsError('Selected study attempt changed')
        binding = job.get('node_submission', {}).get('study')
        if not binding:
            return StudyResult(1, owner['node_id'], owner['queue_id'], '0'*32, 'view', 'NOT_BOUND', '0'*64,
                               json.dumps({'selected_target': target, 'message': 'No immutable study binding', 'cases': []}))
        result = self.view(binding['record_path'])
        if result.study_id != binding['study_id'] or result.plan_digest != binding['plan_digest']:
            raise JobsError('Selected job study binding differs from the original plan')
        payload = json.loads(result.payload_json)
        if not any(row['case_id'] == binding['case_id'] and row.get('target') == target
                   for row in payload['cases']):
            raise JobsError('Selected attempt is not the durably registered study case')
        payload['selected_target'] = target
        return StudyResult(result.schema, result.node_id, result.queue_id, result.study_id, result.operation,
                           result.status, result.plan_digest, json.dumps(payload))

    def compare(self, record_path, selection_path, export_directory=None):
        from .study_comparison import compare_study, inspect_case, export_comparison
        from .study_comparison import validate_comparison_spec
        _, plan = self._record(record_path)
        spec = plan['manifest']; definition = validate_comparison_spec(spec)
        selection_identity = file_identity(_path(selection_path), max_bytes=_LIMIT)
        selection = _read(selection_path)
        if (set(selection) != {'schema', 'study_id', 'plan_digest', 'cases'} or type(selection['schema']) is not int
                or selection['schema'] != 1 or selection['study_id'] != plan['study_id']
                or selection['plan_digest'] != stable_hash(plan) or type(selection['cases']) is not dict
                or set(selection['cases']) - {c['case_id'] for c in plan['preparation']['cases']}):
            raise JobsError('Result selection must explicitly match the study, plan and known case identities')
        rows = []
        for case in plan['preparation']['cases']:
            row = {'case_id': case['case_id'], 'status': 'MISSING', 'reasons': ['No result selected for this case']}
            selected = selection['cases'].get(case['case_id'])
            if selected is not None:
                try:
                    if type(selected) is not dict or set(selected) - {'target', 'spec_path', 'finalization_path'} or not {'target', 'spec_path'} <= set(selected):
                        raise JobsError('Each result requires an exact target and reviewed binding specification')
                    target = selected['target']
                    from cmw.nodes.protocol import target_identity
                    target_identity(target)
                    if any(target[k] != plan[k] for k in ('node_id', 'queue_id')):
                        raise JobsError('Remote result is unavailable on this owner; no cached or cross-host adoption')
                    job, control = read_job(self.store, target['job_id'])
                    expected = {'study_id': plan['study_id'], 'case_id': case['case_id'],
                                'plan_digest': stable_hash(plan), 'record_path': record_path}
                    binding = job.get('node_submission', {}).get('study', {})
                    if job['attempt_id'] != target['attempt_id'] or any(binding.get(k) != v for k,v in expected.items()):
                        raise JobsError('Result target has no matching immutable study-case binding')
                    _, _, registered = self._lookup(plan, case)
                    if not registered or registered['result']['attempt_id'] != target['attempt_id'] or registered['result']['id'] != target['job_id']:
                        raise JobsError('Result is not the recorded registered case attempt; no silent rerun replacement')
                    row.update(target=target, case_binding={'status': 'PASS', 'digest': stable_hash(binding)})
                    closure = _closure(self.store, job, control, self._owner())
                    if closure['closed'] is None:
                        raise JobsError(closure['reason'])
                    if not closure['closed']:
                        raise JobsError('Selected producing domain is not established closed: ' + closure['reason'])
                    runtime = plan['runtimes'][case['case_id']]
                    if job['argv'] != runtime['argv'] or job['cwd'] != runtime['cwd']:
                        raise JobsError('Result execution differs from the approved study runtime')
                    native = runtime['argv'][runtime['argv'].index('--output') + 1]
                    evidence = inspect_case(native, policy_path=spec['scientific_policy']['path'],
                        spec_path=selected['spec_path'], expected_target=target, expected_inputs=case['prepared_inputs'],
                        expected_runtime=case['preparation']['expected_runtime'], metric=spec['metric']['name'],
                        expected_policy_id=definition['policy_id'], finalization_path=selected.get('finalization_path'))
                    row.update(evidence)
                    row['observed'] = True
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    row.update(status='UNAVAILABLE', reasons=[str(exc)[:2048]])
            rows.append(row)
        if file_identity(_path(selection_path), max_bytes=_LIMIT) != selection_identity:
            raise JobsError('Result selection changed during comparison')
        report = compare_study(plan['preparation'], rows)
        report['study_plan_digest'] = stable_hash(plan)
        report['selection_source'] = selection_identity
        if export_directory is not None:
            destination = _path(export_directory)
            protected = [self.store.root, Path(spec['baseline']['directory']), Path(spec['record_root']),
                         *(Path(c['output']) for c in plan['preparation']['cases']),
                         *(Path(r['run_directory']) for r in plan['runtimes'].values())]
            if any(destination == p or destination.is_relative_to(p) or p.is_relative_to(destination) for p in protected):
                raise JobsError('Comparison export overlaps scientific inputs, outputs or Jobs state')
            report['export'] = export_comparison(report, destination)
        status = 'COMPLETE' if all(r.get('observed') is True for r in rows) else 'PARTIAL'
        return self._result(plan, 'compare', status, report)


def launch_blocker(job):
    try:
        binding = job['node_submission']['study']
        record = _read(binding['record_path'])
        service = StudyService(Store(record['plan']['state_directory']))
        record, plan = service._record(binding['record_path'], binding['plan_digest'])
        service._runtime(plan)
        case = _case(plan, binding['case_id']); _prepared(case, plan)
        request, digest, registered = service._lookup(plan, case)
        if (not registered or registered['result']['id'] != job['id']
                or registered['result']['attempt_id'] != job['attempt_id']):
            raise JobsError('Study job is not the durably registered original case attempt')
        if (job['node_submission']['request_id'] != request or job['node_submission']['digest'] != digest
                or binding['study_id'] != plan['study_id'] or job['engine'] != 'VASP'
                or job['resources'] != plan['manifest']['execution']['resources']):
            raise JobsError('Study execution differs from immutable case registration')
        runtime = plan['runtimes'][case['case_id']]
        if (job['argv'], job['cwd'], job['env']) != (runtime['argv'], runtime['cwd'], runtime['env']):
            raise JobsError('Study execution differs from the approved runtime')
        info = _path(job['cwd']).stat()
        parent = _path(runtime['run_directory']).stat()
        if ({'device': info.st_dev, 'inode': info.st_ino} != binding['working_identity']
                or {'device': parent.st_dev, 'inode': parent.st_ino} != binding['run_identity']):
            raise JobsError('Study input or execution-directory identity changed')
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return 'Study preflight: ' + str(exc)
