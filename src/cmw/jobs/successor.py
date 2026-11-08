"""Explicit owner-local planning, no-clobber preparation, and held successors.

A plan is a content-bound review, not an execution request or scientific result.
The engine adapters retain scientific semantics; Store retains attempt numbering
and durable registration. No scheduler or process-control action is used here.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import sqlite3
import stat
import time

from cmw.core.provenance import stable_hash
from .lifecycle import (JobObservationUnavailable, observe_lock, observe_process,
                        queue_read_unavailable, read_job, read_job_with_identity_retry)
from .locations import read_location_metadata
from .runner_assets import file_identity
from .store import JobsError
from .successor_models import SuccessorResult, SuccessorUnverifiable

_FIELDS = {'schema','parent','mode','engine','name','resources','runtime','adapter','approved_changes','checkpoint'}
_CATEGORIES = {'geometry/cell','atom/species order','constraints','k sampling','potentials',
               'Hamiltonian/spin/charge','solver/convergence controls','resources',
               'checkpoint semantics','title_comment_format','missing_evidence','scientific_stage'}


def _path(value):
    if not isinstance(value,str) or not Path(value).is_absolute() or '\0' in value:
        raise JobsError('Successor paths must be explicit absolute owner-node paths')
    path=Path(value)
    if path.resolve(strict=False)!=path:
        raise JobsError('Successor paths cannot traverse symlinks or parent components')
    return path


def _document(path):
    path=_path(str(path))
    identity=file_identity(path,max_bytes=128*1024)
    value=read_location_metadata(path,max_bytes=128*1024)
    if identity!=file_identity(path,max_bytes=128*1024):
        raise JobsError('Successor specification/record changed while reading')
    return value,identity


def _spec(value):
    if type(value) is not dict or set(value)!=_FIELDS or type(value['schema']) is not int or value['schema']!=1:
        raise JobsError('Successor specification requires exactly the schema-1 fields')
    if (type(value['mode']) is not str or type(value['engine']) is not str or
            value['mode'] not in {'retry','geometry','next-stage'} or value['engine'] not in {'VASP','ORCA'}):
        raise JobsError('Unsupported successor mode or engine')
    if value['checkpoint']!='none':
        raise JobsError('Electronic checkpoint reuse is unsupported; select checkpoint none')
    parent=value['parent']
    if type(parent) is not dict or set(parent)!={'node_id','queue_id','job_id','attempt_id'}:
        raise JobsError('Successor requires an exact global parent target')
    from cmw.nodes.protocol import target_identity
    try:
        target_identity(parent)
    except ValueError as exc:
        raise JobsError(str(exc)) from exc
    if not isinstance(value['name'],str) or not value['name'].strip() or len(value['name'])>256 or '\0' in value['name']:
        raise JobsError('Successor name must be bounded nonempty text')
    resources=value['resources']
    if type(resources) is not dict or set(resources)!={'cpus','memory_gib','mpi_ranks','threads_per_rank'}:
        raise JobsError('Successor requires all explicit resource requests')
    for key,amount in resources.items():
        if type(amount) not in (int,float) or not math.isfinite(amount) or amount<=0 or (key!='memory_gib' and type(amount) is not int):
            raise JobsError('Successor resources must be positive finite values')
    if resources['cpus']<resources['mpi_ranks']*resources['threads_per_rank']:
        raise JobsError('Successor CPU request understates its ranks/threads')
    if type(value['runtime']) is not dict or type(value['adapter']) is not dict:
        raise JobsError('Caller-owned runtime and engine adapter specifications are required')
    _adapter_shape(value)
    changes=value['approved_changes']
    if type(changes) is not list or len(changes)>len(_CATEGORIES) or any(type(c) is not str or c not in _CATEGORIES for c in changes) or len(set(changes))!=len(changes):
        raise JobsError('approved_changes must contain unique supported semantic categories')
    try:
        json.dumps(value,allow_nan=False)
    except (ValueError,TypeError) as exc:
        raise JobsError('Successor specification must contain finite JSON values') from exc
    return deepcopy(value)


def _adapter_shape(spec):
    """Refuse malformed containers before an adapter can index or open a source."""
    adapter, runtime = spec['adapter'], spec['runtime']
    if spec['engine'] == 'VASP':
        from .successor_vasp import _OPTIONS, _REQUIRED
        if not _REQUIRED <= set(adapter) or set(adapter) - _OPTIONS:
            raise JobsError('VASP adapter has missing or unsupported fields')
        text_fields = set(adapter) - {'segment', 'allow_structure_changes'}
        if (set(runtime) != {'node_config', 'profile', 'vasp', 'run_directory'} or
                type(runtime.get('vasp')) is not dict or any(type(runtime[key]) is not str
                    for key in ('node_config', 'profile', 'run_directory'))):
            raise JobsError('VASP runtime requires node_config, profile, vasp options and new run_directory')
        allowed = adapter.get('allow_structure_changes', [])
        if type(allowed) is not list or any(type(item) is not str for item in allowed):
            raise JobsError('VASP structure changes must be a list of explicit names')
    else:
        if set(adapter) != {'source', 'template_input', 'execution_config', 'execution_profile', 'output'}:
            raise JobsError('ORCA adapter requires source, template_input, execution_config, execution_profile and output')
        source = adapter['source']
        if (type(source) is not dict or not {'mode', 'input', 'target', 'geometry'} <= set(source) or
                set(source) - {'mode', 'input', 'target', 'geometry', 'output', 'result', 'evaluation'} or
                any(type(item) is not str for key,item in source.items() if key != 'evaluation')):
            raise JobsError('ORCA source requires explicitly named input, target, geometry and mode')
        required = {'executable', 'sha256', 'version', 'qualification'}
        if (not required <= set(runtime) or set(runtime) - (required | {'contract'}) or
                any(type(item) is not str for item in runtime.values())):
            raise JobsError('ORCA runtime requires recorded executable, sha256, version and qualification strings')
        text_fields = set(adapter) - {'source'}
    if any(type(adapter[key]) is not str or not adapter[key] or len(adapter[key]) > 4096 or '\0' in adapter[key]
           for key in text_fields):
        raise JobsError('Successor adapter paths, profiles and source modes must be bounded nonempty text')


def _closure(store,job,control,owner):
    """Retain native completion, plus current lock and original-domain absence."""
    from .ownership import group_members
    from cmw.periodic.vasp.result_execution import inspect_execution_snapshot
    if job['status'] not in {'Done','Fail','Cancelled'}:
        return {'closed':False,'reason':'Parent is active, unstarted or Unknown; drainage is not established'}
    receipt=store.root/'attempts'/job['attempt_id']/'payload-exit.json'
    snapshot={'schema':control['schema'],'state_directory':str(store.root),'jobs':[job],
              'queue_identity':owner,'controller':control,'observed_at':time.time()}
    target={k:owner[k] for k in ('node_id','queue_id')}
    target.update(job_id=job['id'],attempt_id=job['attempt_id'])
    observed=inspect_execution_snapshot(snapshot,receipt,job_id=job['id'],attempt_id=job['attempt_id'],
                                        run_directory=job['cwd'],expected_target=target)
    # A nonzero/cancelled native producer can still yield an explicitly reviewed
    # complete geometry. These success tests are separate from closure checks.
    success_only={'terminal_done','exit_zero','no_signal','not_cancelled','not_terminated','suspension_terminal'}
    checks = {name: value for name, value in observed['checks'].items()
              if name not in success_only and value is not True}
    if job.get('worker_protocol') != 3:
        checks['worker_protocol'] = False
    contradicted = [name for name, value in checks.items() if value is False]
    if contradicted:
        return {'closed': False, 'reason': 'Parent closure contradicted: ' + ', '.join(contradicted),
                'checks': checks}
    worker = observe_process(job.get('worker')).verdict
    anchor = observe_process(job.get('group')).verdict
    lock = observe_lock(store, job['attempt_id']).verdict
    authorities = {'worker': worker, 'anchor': anchor, 'lock': lock}
    if worker in {'matched', 'mismatch'} or anchor in {'matched', 'mismatch'} or lock == 'held':
        return {'closed': False, 'reason': 'Original worker, domain or lock is not confirmed closed',
                **authorities}
    unavailable = [f'{name} ({verdict})' for name, verdict in authorities.items()
                   if verdict not in {'gone', 'unheld'}]
    if anchor == 'gone':
        try:
            if group_members(job['group']['pid'], session=True):
                return {'closed': False, 'reason': 'Original payload session still has live members'}
        except (JobsError, OSError) as exc:
            unavailable.append(f'original payload session ({exc})')
    if checks or unavailable:
        missing = [*checks, *unavailable]
        live = observed.get('live_queue') or {}
        if live.get('status') == 'UNAVAILABLE':
            missing.insert(0, 'parent queue temporarily unreadable: ' + live['reason'])
        return {'closed': None, 'reason': 'Parent closure unverifiable, retry: ' + ', '.join(missing),
                'checks': checks, **authorities}
    try:
        receipt_identity = file_identity(receipt, max_bytes=65536)
    except OSError as exc:
        return {'closed': None, 'reason': f'Parent receipt unverifiable, retry: {exc}'}
    return {'closed':True,'receipt':receipt_identity,'status':job['status'],
            'exit_code':job['exit_code'],'worker':job['worker'],'group':job['group'],
            'started_at':job['started_at'],'finished_at':job['finished_at'],
            'qualification':'Native terminal receipt and original domain absence; not scientific acceptance'}


def _record_path(spec):
    adapter = spec['adapter']
    if spec['engine'] == 'ORCA':
        return _path(adapter['output']) / 'successor.json'
    scratch = Path(adapter['scratch_root']).expanduser().absolute()
    directory = _path(str(scratch / Path(adapter['record_directory']).expanduser()))
    if directory == scratch or not directory.is_relative_to(scratch):
        raise JobsError('Successor record directory must be inside its Scratch root')
    return directory / 'successor.json'


@contextmanager
def _publication_lock(directory):
    # The private publication directory remains in place when successor.json is replaced.
    import fcntl
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _save_record(path, value, directory_fd, *, create=False):
    locked = os.fstat(directory_fd)
    if _directory_identity(path.parent) != [locked.st_dev, locked.st_ino]:
        raise JobsError('Successor publication directory identity changed')
    raw = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > 128 * 1024:
        raise JobsError('Successor record exceeds 128 KiB')
    fd, name = tempfile.mkstemp(prefix='.successor-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(name, path)  # No-clobber publication of a complete document.
        else:
            os.replace(name, path)
        os.fsync(directory_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def _entry(path):
    """Only ENOENT establishes absence; permission/read errors must refuse."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _directory_identity(path):
    observed = _path(str(path)).lstat()
    if not stat.S_ISDIR(observed.st_mode):
        raise JobsError('Successor destination is not a direct directory: ' + str(path))
    return [observed.st_dev, observed.st_ino]


def _recover_publication(path, lock, spec, spec_identity, approved_digest):
    """Read the durable pre-record evidence; never adopt an unbound directory."""
    preparation_path = path.parent / 'preparation.json'
    materialized, _ = _document(preparation_path)
    materialized = deepcopy(materialized)
    recovery = materialized.pop('successor_recovery', None)
    if not isinstance(recovery, dict) or recovery.get('schema') != 1:
        raise JobsError('Publication lacks approved recovery evidence; use fresh destinations and review a new plan')
    record = {**recovery, 'schema': 1, 'kind': 'cmw-successor-materialization',
              'status': 'PENDING_VERIFICATION', 'materialized': materialized}
    plan = record['plan']
    if not isinstance(plan, dict):
        raise JobsError('Malformed successor publication recovery evidence')
    if (record.get('plan_digest') != approved_digest or stable_hash(plan) != approved_digest
            or plan.get('spec') != spec or plan.get('specification_source') != spec_identity
            or record.get('record_path') != str(path)):
        raise JobsError('Publication recovery evidence does not match this approved request')
    if plan['spec']['engine'] != 'VASP' or plan.get('status') != 'READY':
        raise JobsError('Publication recovery requires an approved READY VASP plan')
    publication = plan['adapter']['publication_plan']
    expected = {**plan['adapter']['preparation'], 'schema_version': 1,
                'successor': {'schema_version': 1, 'parent': plan['parent'],
                    'source': plan['adapter']['source'], 'plan_digest': plan['adapter']['digest'],
                    'relationship': 'new-input-guess', 'checkpoint_reuse': False}}
    actual = {key: value for key, value in materialized.items()
              if key not in {'preparation_id', 'publication'}}
    location = materialized.get('publication', {})
    if (actual != expected or location.get('state') != 'complete'
            or any(location.get(key) != publication[key]
                   for key in ('kind', 'input_path', 'record_path', 'scratch_root'))):
        raise JobsError('Preparation evidence differs from the approved complete publication')
    directory = os.fstat(lock)
    if record['publication_directory'] != [directory.st_dev, directory.st_ino]:
        raise JobsError('Successor publication directory identity changed')
    identities = ((path.parent, record['publication_directory']),
                  (Path(publication['input_path']), record['input_directory']),
                  (Path(plan['runtime']['cwd']), record['working_identity']),
                  (Path(publication['input_path']).parent, publication['input_parent_identity']),
                  (path.parent.parent, publication['record_parent_identity']))
    for directory_path, identity in identities:
        if _directory_identity(directory_path) != identity:
            raise JobsError('Successor destination directory identity changed: ' + str(directory_path))
    for directory_path, names in ((path.parent, {'preparation.json'}),
                                  (Path(plan['runtime']['cwd']), set())):
        with os.scandir(directory_path) as entries:
            if {entry.name for entry in entries} != names:
                raise JobsError('Successor destination contains unexpected entries: ' + str(directory_path))
    record['prepared'] = _expected_inputs(plan, materialized)
    return record


def _expected_inputs(plan, materialized):
    if plan['spec']['engine'] == 'ORCA':
        return materialized['prepared_plan']['dependencies']
    root = Path(plan['spec']['adapter']['output'])
    return {name: {'path': str(root / name), **identity}
            for name, identity in plan['adapter']['preparation']['prepared_inputs'].items()}


def _check_published_inputs(record):
    plan = record['plan']
    expected = _expected_inputs(plan, record['materialized'])
    if record['prepared'] != expected:
        raise JobsError('Recorded successor inputs differ from the approved publication')
    if plan['spec']['engine'] == 'ORCA':
        from .molecular import _verified_dependencies
        _verified_dependencies({'dependencies': expected})
        return
    output = _path(plan['spec']['adapter']['output'])
    with os.scandir(output) as entries:
        names = {entry.name for entry in entries}
        if names != set(expected):
            raise JobsError('Published VASP input names differ from approval: missing=' +
                            str(sorted(set(expected) - names)) + ', extra=' + str(sorted(names - set(expected))))
    for name, identity in expected.items():
        path = _path(identity['path'])
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise JobsError('Published VASP input is not a regular file: ' + name)
        actual = file_identity(path, max_bytes=64*1024*1024)
        after = path.lstat()
        if any(getattr(before, field) != getattr(after, field) for field in
               ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise JobsError('Published VASP input changed while reading: ' + name)
        if actual != identity:
            raise JobsError('Published VASP input differs from approved bytes: ' + name)
    work = _path(plan['runtime']['cwd']).stat()
    if record['working_identity'] != [work.st_dev, work.st_ino]:
        raise JobsError('Published successor working directory changed')


def _require_vasp_readiness(config):
    from cmw.nodes.identity import check_node
    readiness = check_node(config)
    if not readiness['ready']:
        reason = '; '.join(readiness['errors'])
        if readiness.get('queue_read_unavailable'):
            raise SuccessorUnverifiable(f'VASP profile queue temporarily unreadable; unverifiable, retry: {reason}')
        raise JobsError(f'VASP runtime profile is not ready: {reason}')


class SuccessorService:
    def __init__(self,store,origin=None):
        self.store,self.origin=store,dict(origin or {})

    def _parent(self,job_id,expected_attempt_id):
        try:
            job, control, owner = read_job_with_identity_retry(self.store, job_id)
        except JobObservationUnavailable as exc:
            raise SuccessorUnverifiable(f'Parent queue temporarily unreadable; unverifiable, retry: {exc}') from exc
        except sqlite3.Error as exc:
            raise JobsError(f'Parent queue is malformed: {exc}') from exc
        if expected_attempt_id is not None and job['attempt_id']!=expected_attempt_id:
            raise JobsError('Successor parent attempt changed')
        if not owner:
            raise JobsError('Successor requires an existing enrolled owner queue; no automatic enrollment')
        if any(k in self.origin and self.origin[k]!=owner[k] for k in ('node_id','queue_id','host_id')):
            raise JobsError('Successor owner identity changed')
        target={k:owner[k] for k in ('node_id','queue_id')}
        target.update(job_id=job['id'],attempt_id=job['attempt_id'])
        return job,control,owner,target

    @staticmethod
    def _result(target,operation,status,digest,payload):
        return SuccessorResult(1,**target,operation=operation,status=status,plan_digest=digest,
                               payload_json=json.dumps(payload,sort_keys=True,allow_nan=False))

    def _runtime(self,spec,target):
        if spec['engine']=='ORCA':
            from cmw.nodes.identity import runtime_identity
            return {'kind':'ordinary-orca-reviewed-runtime','specification':spec['runtime'],
                    'runtime_id':runtime_identity()['fingerprint']}
        value=spec['runtime']
        if set(value)!={'node_config','profile','vasp','run_directory'}:
            raise JobsError('VASP runtime requires node_config, profile, vasp options and new run_directory')
        from cmw.nodes.identity import load_node
        config=load_node(_path(value['node_config']))
        if config['node_id']!=target['node_id'] or not any(q['queue_id']==target['queue_id'] and q['state']==str(self.store.root) for q in config['queues'].values()):
            raise JobsError('VASP runtime profile belongs to another owner queue')
        profile=config['profiles'].get(value['profile'])
        if not profile or profile['engine']!='VASP':
            raise JobsError('An enrolled VASP runtime profile is required')
        if spec['resources']['cpus']>profile['max_cpus'] or spec['resources']['mpi_ranks']>profile['max_ranks']:
            raise JobsError('Successor resources exceed the pinned VASP profile')
        _require_vasp_readiness(config)
        from cmw.nodes.submission import _argv
        output=_path(spec['adapter']['output']);run=_path(value['run_directory'])
        if run.exists() or not run.parent.is_dir() or run==output or run.is_relative_to(output) or output.is_relative_to(run):
            raise JobsError('VASP execution directory must be new and separate from the canonical inputs')
        argv=_argv({'engine':'VASP','vasp':value['vasp'],'resources':spec['resources']},profile,
                   output.parent,run,run/'native',input_directory=output)
        from cmw.nodes.runner_profiles import runner_environment
        return {'kind':'enrolled-vasp-profile','profile':value['profile'],'profile_record':profile,
                'runtime_id':config['runtime']['fingerprint'],'argv':argv,'cwd':str(run),
                'env':{**runner_environment(profile),'CMW_JOBS_OWN_SESSION':'1','OMP_NUM_THREADS':'1'}}

    def _build(self,job_id,spec_path,expected_attempt_id=None):
        value,spec_identity=_document(spec_path);spec=_spec(value)
        job,control,owner,target=self._parent(job_id,expected_attempt_id)
        if spec['parent']!=target or spec['engine']!=job['engine']:
            raise JobsError('Successor spec selects a different owner, attempt or engine')
        closure=_closure(self.store,job,control,owner)
        plan={'schema':1,'kind':'cmw-successor-plan','spec':spec,'specification_source':spec_identity,
              'parent':target,'state_directory':str(self.store.root),'closure':closure,
              'checkpoint':{'requested':'none','reuse':'REFUSED','reason':'No complete electronic-checkpoint validator is claimed'},
              'execution':'Preparation only; separate held registration, release and dispatch'}
        if closure['closed'] is None:
            plan.update(status='UNVERIFIABLE', reason=closure['reason'])
            return plan
        if not closure['closed']:
            plan['status'] = 'BLOCKED'
            return plan
        from .review_worker import _directory
        if spec['engine']=='VASP':
            if _path(spec['adapter']['directory'])!=Path(_directory(self.store,job)).resolve():
                raise JobsError('Selected VASP directory is not the parent attempt declared output')
            from .successor_vasp import preview_vasp
            try:
                preview = preview_vasp(spec['adapter'], target, state_directory=str(self.store.root))
            except SuccessorUnverifiable as exc:
                plan.update(status='UNVERIFIABLE', reason=str(exc))
                return plan
        else:
            from .successor_orca import preview_orca
            preview=preview_orca(spec['adapter'],target,job=job,state_directory=str(self.store.root),
                                 runtime=spec['runtime'],resources=spec['resources'])
        source_mode=spec['adapter'].get('source_mode',spec['adapter'].get('source',{}).get('mode'))
        if (spec['mode']=='retry')!=(source_mode=='input'):
            raise JobsError('Retry uses reviewed original inputs; geometry/next-stage uses an observed or accepted geometry')
        if spec['engine']=='VASP':
            declared=preview['preparation']['expected_runtime']
            if any(declared.get(key)!=spec['resources'][key] for key in ('cpus','mpi_ranks','threads_per_rank')):
                raise JobsError('Prepared VASP runtime resources differ from the explicit successor commitments')
            overlay=declared.get('incar',{})
            if any(overlay.get(tag)!=spec['runtime']['vasp'].get(option) for tag,option in (('NCORE','ncore'),('KPAR','kpar'))):
                raise JobsError('Prepared VASP runtime overlay differs from the explicit runner options')
            if spec['resources']['threads_per_rank']!=1:
                raise JobsError('VASP successors currently support one explicit OpenMP thread per rank')
        try:
            runtime = self._runtime(spec, target)
        except SuccessorUnverifiable as exc:
            plan.update(status='UNVERIFIABLE', reason=str(exc))
            return plan
        protected=[Path(job['cwd']).resolve(),self.store.root.resolve()]
        if job.get('molecular_submission'):
            protected.append(Path(job['molecular_submission']['spec']['workdir']).resolve())
        destinations=[_path(spec['adapter']['output'])]
        if spec['engine']=='VASP':
            protected.append(_path(spec['adapter']['directory']))
            destinations.extend([_path(runtime['cwd']),Path(preview['publication_plan']['record_directory'])])
        for destination in destinations:
            if any(destination==root or destination.is_relative_to(root) or root.is_relative_to(destination) for root in protected):
                raise JobsError('Successor destination overlaps its parent or Jobs state')
        differences=preview.get('differences',preview.get('changes',[]))
        if type(differences) is not list:
            raise JobsError('Engine adapter lacks structured semantic differences')
        differences=deepcopy(differences)
        if job['resources']!=spec['resources']:
            differences.append({'successor_category':'resources','before':job['resources'],'after':spec['resources']})
        categories={d.get('successor_category',d.get('category')) for d in differences}
        unapproved=categories-set(spec['approved_changes'])
        if unapproved:
            raise JobsError('Explicit approved_changes missing: '+', '.join(sorted(str(c) for c in unapproved)))
        if spec['mode']=='retry' and categories-{'resources','title_comment_format','missing_evidence'}:
            raise JobsError('Retry must retain the reviewed scientific inputs; choose explicit geometry or next-stage intent')
        plan.update(status='READY',adapter=preview,runtime=runtime,differences=differences)
        return plan

    def plan(self,job_id,spec_path,expected_attempt_id=None):
        plan=self._build(job_id,spec_path,expected_attempt_id)
        return self._result(plan['parent'],'plan',plan['status'],stable_hash(plan),plan)

    def materialize(self,job_id,spec_path,approved_digest,expected_attempt_id=None):
        value, spec_identity = _document(spec_path)
        spec = _spec(value)
        path = _record_path(spec)
        if _entry(path) is not None or (spec['engine'] == 'VASP' and _entry(path.parent) is not None):
            with _publication_lock(path.parent) as lock:
                recovering = _entry(path) is None
                if recovering:
                    try:
                        record = _recover_publication(path, lock, spec, spec_identity, approved_digest)
                    except (KeyError, TypeError, AttributeError) as exc:
                        raise JobsError('Malformed successor publication recovery evidence') from exc
                else:
                    record, _ = _document(path)
                if 'parent_selector' not in record:
                    raise JobsError('Existing publication does not support materialization replay; inspect its registration record')
                plan = record.get('plan') or {}
                target = spec['parent']
                if (record.get('kind') != 'cmw-successor-materialization'
                        or record.get('record_path') != str(path)
                        or record.get('plan_digest') != approved_digest or stable_hash(plan) != approved_digest
                        or plan.get('spec') != spec or plan.get('specification_source') != spec_identity
                        or plan.get('parent') != target or plan.get('state_directory') != str(self.store.root)
                        or str(job_id) not in {str(target['job_id']), record.get('parent_display_id'),
                                              record.get('parent_selector')}
                        or expected_attempt_id not in (None, target['attempt_id'])
                        or any(k in self.origin and self.origin[k] != target[k] for k in ('node_id', 'queue_id'))):
                    raise JobsError('Existing successor record does not match this approved request')
                observed = os.fstat(lock)
                if record.get('publication_directory') != [observed.st_dev, observed.st_ino]:
                    raise JobsError('Successor publication directory identity changed')
                if spec['engine'] == 'VASP':
                    mount = plan['adapter']['publication_plan']['scratch_mount']
                    if mount is not None and not os.path.ismount(_path(mount)):
                        raise SuccessorUnverifiable('Original Scratch mount unavailable; retry on its owner')
                if record['status'] not in {'PENDING_VERIFICATION', 'MATERIALIZED'}:
                    raise JobsError('Existing successor materialization is incomplete')
                # A display label must still resolve to this immutable parent.
                selector_error = None
                if str(job_id) != str(target['job_id']):
                    try:
                        _, _, _, selected = self._parent(job_id, target['attempt_id'])
                    except SuccessorUnverifiable as exc:
                        selector_error = exc
                    else:
                        if selected != target:
                            raise JobsError('Current job selector differs from the approved parent')
                if recovering:
                    # Even unavailable parent evidence cannot excuse changed sources.
                    from .successor_vasp import _verify_sources
                    _verify_sources(plan['adapter'])
                return self._finish_publication(path, record, lock, retry=True,
                                                selector_error=selector_error, create=recovering)

        plan = self._build(job_id, spec_path, expected_attempt_id)
        if plan['status'] == 'UNVERIFIABLE':
            raise SuccessorUnverifiable(plan['reason'])
        digest = stable_hash(plan)
        if plan['status'] != 'READY' or digest != approved_digest or plan['spec'] != spec:
            raise JobsError('Successor plan changed or is blocked; review the new plan digest')
        target = plan['parent']
        job, _, _, _ = self._parent(job_id, target['attempt_id'])
        if spec['engine'] == 'VASP':
            from .successor_vasp import materialize_vasp
            materialized = materialize_vasp(spec['adapter'], target, plan['adapter'], state_directory=str(self.store.root))
        else:
            from .successor_orca import materialize_orca
            materialized = materialize_orca(spec['adapter'], target, plan['adapter'], job=job,
                state_directory=str(self.store.root), runtime=spec['runtime'], resources=spec['resources'])
        record = {'schema': 1, 'kind': 'cmw-successor-materialization', 'status': 'PENDING_VERIFICATION',
                  'record_path': str(path), 'plan_digest': digest, 'plan': plan, 'materialized': materialized,
                  'parent_selector': str(job_id), 'parent_display_id': job['display_id'],
                  'prepared': _expected_inputs(plan, materialized)}
        with _publication_lock(path.parent) as lock:
            directory = os.fstat(lock)
            record['publication_directory'] = [directory.st_dev, directory.st_ino]
            if spec['engine'] == 'VASP':
                try:
                    work = _path(plan['runtime']['cwd'])
                    work.mkdir(mode=0o700)
                    created = work.stat()
                    record['working_identity'] = [created.st_dev, created.st_ino]
                except (OSError, ValueError) as exc:
                    record.update(status='INCOMPLETE', error=str(exc)[:2048])
                    _save_record(path, record, lock, create=True)
                    raise
                # Retain approval and created-directory identities before the first
                # successor record write, so ENOSPC there need not strand inputs.
                recovery = {key: record[key] for key in ('plan', 'plan_digest', 'record_path',
                            'parent_selector', 'parent_display_id', 'publication_directory', 'working_identity')}
                recovery.update(schema=1, input_directory=_directory_identity(Path(spec['adapter']['output'])))
                preparation_path = path.parent / 'preparation.json'
                published, _ = _document(preparation_path)
                if published != materialized:
                    raise JobsError('Preparation evidence changed before saving successor recovery approval')
                _save_record(preparation_path,
                             {**materialized, 'successor_recovery': recovery}, lock)
            _save_record(path, record, lock, create=True)
            return self._finish_publication(path, record, lock, retry=False)

    def _finish_publication(self, path, record, lock, *, retry, selector_error=None, create=False):
        plan = record['plan']
        target, spec = plan['parent'], plan['spec']
        completed = record['status'] == 'MATERIALIZED'
        try:
            _check_published_inputs(record)
            if selector_error is not None:
                raise selector_error
            current, control, owner, observed = self._parent(target['job_id'], target['attempt_id'])
            if target != observed:
                raise JobsError('Parent identity changed during materialization')
            closure = _closure(self.store, current, control, owner)
            if closure['closed'] is None:
                raise SuccessorUnverifiable(closure['reason'])
            if closure != plan['closure']:
                raise JobsError('Parent closure changed during materialization; inputs retained without registration')
            if spec['engine'] == 'VASP':
                from cmw.periodic.vasp.inputs import check_inputs
                if check_inputs(spec['adapter']['output'])['status'] != 'valid':
                    raise JobsError('Published VASP four-input check did not pass')
                from .successor_vasp import verify_published_parent
                verify_published_parent(spec['adapter'], target, str(self.store.root), plan['adapter'],
                                        recheck_sources=retry)
            elif retry:
                from .successor_orca import verify_materialized_orca
                verify_materialized_orca(spec['adapter'], target, plan['adapter'], record['materialized'],
                    job=current, state_directory=str(self.store.root), runtime=spec['runtime'], resources=spec['resources'])
        except SuccessorUnverifiable as exc:
            if completed:
                raise
            if create:
                _save_record(path, record, lock, create=True)
            return self._publication_result(record, reason=str(exc))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if not completed and not create:
                record.update(status='INCOMPLETE', error=str(exc)[:2048])
                _save_record(path, record, lock)
            raise
        if completed:
            return self._publication_result(record, replayed=True)
        record['status'] = 'MATERIALIZED'
        _save_record(path, record, lock, create=create)
        return self._publication_result(record)

    def _publication_result(self, record, *, replayed=False, reason=None):
        payload = {'record_path': record['record_path'], 'registration': 'not requested', 'replayed': replayed}
        if reason is None:
            payload['materialized'] = record['materialized']
        else:
            payload['reason'] = reason
        return self._result(record['plan']['parent'], 'materialize', record['status'], record['plan_digest'], payload)

    def register(self,job_id,record_path,approved_digest,request_id,expected_attempt_id=None):
        try:
            return self._register(job_id,record_path,approved_digest,request_id,expected_attempt_id)
        except (KeyError,TypeError,AttributeError) as exc:
            raise JobsError('Malformed successor materialization record') from exc

    def _register(self,job_id,record_path,approved_digest,request_id,expected_attempt_id=None):
        if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):
            raise JobsError('Registration requires a new lowercase UUID hex request identity')
        record,_=_document(record_path)
        if (record.get('kind')!='cmw-successor-materialization' or record.get('status') not in {'MATERIALIZED','PENDING_VERIFICATION'}
                or record.get('record_path')!=str(_path(record_path)) or record.get('plan_digest')!=approved_digest
                or stable_hash(record.get('plan'))!=approved_digest):
            raise JobsError('Materialization record or approved plan identity is invalid')
        plan=record['plan'];spec=_spec(plan['spec'])
        expected_record=(Path(plan['adapter']['publication_plan']['record_directory'])/'successor.json'
                         if spec['engine']=='VASP' else Path(spec['adapter']['output'])/'successor.json')
        if _path(record_path)!=expected_record:
            raise JobsError('Successor record must remain at its approved original publication location')
        job,control,owner,target=self._parent(job_id,expected_attempt_id)
        if target!=plan['parent'] or target!=spec['parent'] or plan['state_directory']!=str(self.store.root):
            raise JobsError('Materialization belongs to a different original owner/parent')
        if record['status'] == 'PENDING_VERIFICATION':
            raise SuccessorUnverifiable('Parent verification pending; retry materialize-next before registration')
        registration_digest=stable_hash({'plan_digest':approved_digest,'record_path':str(record_path)})
        try:
            previous = self.store.request_lookup(request_id)
        except (JobsError, OSError, sqlite3.Error) as exc:
            if queue_read_unavailable(exc):
                raise SuccessorUnverifiable(f'Parent queue receipt unverifiable, retry: {exc}') from exc
            raise
        if previous:
            if previous['operation']!='submit' or previous['digest']!=registration_digest:
                raise JobsError('Registration request ID belongs to a different successor')
            return self._registered(target,approved_digest,previous['result'],True)
        closure = _closure(self.store, job, control, owner)
        if closure['closed'] is None:
            raise SuccessorUnverifiable(closure['reason'])
        if closure != plan['closure']:
            raise JobsError('Parent closure changed since approved materialization')
        from .molecular import _verified_dependencies
        if spec['engine']=='VASP':
            from .successor_vasp import verify_published_parent
            verify_published_parent(spec['adapter'], target, str(self.store.root), plan['adapter'],
                                    recheck_sources=False)
            expected=plan['adapter']['preparation']['prepared_inputs']
            prepared={name:file_identity(Path(spec['adapter']['output'])/name,max_bytes=64*1024*1024)
                      for name in ('INCAR','POSCAR','KPOINTS','POTCAR')}
            for name,identity in prepared.items():
                if identity['sha256']!=expected[name]['sha256']:
                    raise JobsError('Prepared VASP bytes differ from the exact approved inputs')
            if record['prepared']!=prepared:
                raise JobsError('Materialized VASP dependency binding differs from approved input paths')
            from cmw.periodic.vasp.inputs import check_inputs
            if check_inputs(spec['adapter']['output'])['status']!='valid':
                raise JobsError('Prepared VASP input check failed before registration')
        else:
            from .successor_orca import verify_materialized_orca
            record['materialized']=verify_materialized_orca(spec['adapter'],target,plan['adapter'],record['materialized'],
                job=job,state_directory=str(self.store.root),runtime=spec['runtime'],resources=spec['resources'])
            if record['prepared']!=record['materialized']['prepared_plan']['dependencies']:
                raise JobsError('Materialized ORCA dependency closure differs from its verified plan')
            _verified_dependencies({'dependencies':record['prepared']})
        node={'schema':1,**{k:target[k] for k in ('node_id','queue_id')},'request_id':request_id,
              'digest':registration_digest,'successor':{'plan_digest':approved_digest,'record_path':str(record_path),
              'parent':target,'mode':spec['mode']},'runtime_id':plan['runtime'].get('runtime_id','')}
        kwargs={}
        if spec['engine']=='VASP':
            runtime=plan['runtime'];argv,cwd,env=runtime['argv'],runtime['cwd'],runtime['env']
            from cmw.nodes.identity import load_node
            config=load_node(spec['runtime']['node_config'])
            if config['profiles'].get(spec['runtime']['profile']) != runtime['profile_record']:
                raise JobsError('VASP runtime profile changed before registration')
            _require_vasp_readiness(config)
        else:
            from .molecular import binding
            data=record['materialized']
            molecular_spec={'schema':1,'request_id':request_id,'engine':'ORCA','name':spec['name'],
                'intent':'held','resources':spec['resources'],'workdir':data['workdir'],
                'runtime':spec['runtime'],'scientific':data['scientific']}
            bound=binding(molecular_spec,data['prepared_plan'])
            execution=bound['plan'];argv,cwd,env=execution['argv'],execution['cwd'],execution['env']
            from cmw.progress.models import SourceDescriptor
            kwargs={'molecular_submission':bound,'progress_sources':tuple(SourceDescriptor(**s) for s in execution['progress_sources'])}
        work=Path(cwd).stat()
        node['successor']['working_identity']={'device':work.st_dev,'inode':work.st_ino}
        child=self.store.add(argv=argv,cwd=cwd,env=env,name=spec['name'],engine=spec['engine'],
            **spec['resources'],hold=True,remote_request={'request_id':request_id,'digest':registration_digest},
            node_submission=node,**kwargs)
        return self._registered(target,approved_digest,child,False)

    def _registered(self,target,digest,child,replayed):
        return self._result(target,'register','REGISTERED',digest,{'replayed':replayed,
            'child':{**{k:target[k] for k in ('node_id','queue_id')},'job_id':child['id'],
                     'attempt_id':child['attempt_id'],'display_id':child['display_id'],'status':child['status']},
            'execution':'Held new attempt only; release and dispatch remain separate'})


def launch_blocker(job):
    """Recheck an owner-local successor through the existing final-admission hook."""
    try:
        from cmw.nodes.identity import runtime_identity, load_node, check_node
        from .store import Store
        binding=job['node_submission']; successor=binding['successor']
        record,_=_document(successor['record_path']); plan=record['plan']; spec=_spec(plan['spec'])
        if (record.get('status')!='MATERIALIZED' or record.get('record_path')!=successor['record_path']
                or stable_hash(plan)!=successor['plan_digest'] or record['plan_digest']!=successor['plan_digest']
                or successor['parent']!=plan['parent']):
            raise JobsError('Original approved successor record changed')
        expected_record=(Path(plan['adapter']['publication_plan']['record_directory'])/'successor.json'
                         if spec['engine']=='VASP' else Path(spec['adapter']['output'])/'successor.json')
        if str(expected_record)!=successor['record_path']:
            raise JobsError('Successor record moved from its original publication location')
        store=Store(plan['state_directory'])
        owner=store.queue_identity()
        if not owner or any(binding[k]!=owner[k] or plan['parent'][k]!=owner[k] for k in ('node_id','queue_id')):
            raise JobsError('Original successor owner queue changed')
        if runtime_identity()['fingerprint']!=binding['runtime_id'] or binding['runtime_id']!=plan['runtime']['runtime_id']:
            raise JobsError('Approved successor CMW runtime changed')
        work=_path(job['cwd']); observed=work.stat()
        if {'device':observed.st_dev,'inode':observed.st_ino}!=successor['working_identity']:
            raise JobsError('Prepared successor working directory changed')
        if job['resources']!=spec['resources'] or job['engine']!=spec['engine']:
            raise JobsError('Successor execution differs from approved resources or engine')
        if spec['engine']=='VASP':
            runtime=plan['runtime']
            if (job['argv'],job['cwd'],job['env'])!=(runtime['argv'],runtime['cwd'],runtime['env']):
                raise JobsError('Successor execution differs from the approved VASP runtime')
            config=load_node(spec['runtime']['node_config'])
            if (not check_node(config)['ready'] or config['node_id']!=binding['node_id']
                    or config['profiles'].get(spec['runtime']['profile'])!=runtime['profile_record']):
                raise JobsError('VASP successor runtime profile changed')
            from cmw.nodes.runner_profiles import runner_environment
            expected_env = {**runner_environment(config['profiles'][runtime['profile']]),
                            'CMW_JOBS_OWN_SESSION': '1', 'OMP_NUM_THREADS': '1'}
            if runtime['env'] != expected_env:
                raise JobsError('VASP successor interpreter environment differs from its profile pin')
            expected=plan['adapter']['preparation']['prepared_inputs']
            for name in ('INCAR','POSCAR','KPOINTS','POTCAR'):
                path=_path(str(Path(spec['adapter']['output'])/name))
                if file_identity(path,max_bytes=64*1024*1024)['sha256']!=expected[name]['sha256']:
                    raise JobsError('Approved VASP successor input changed: '+name)
        else:
            from .successor_orca import verify_materialized_orca
            parent,_=read_job(store,plan['parent']['job_id'])
            materialized=verify_materialized_orca(spec['adapter'],plan['parent'],plan['adapter'],record['materialized'],
                job=parent,state_directory=str(store.root),runtime=spec['runtime'],resources=spec['resources'])
            from .molecular import binding as bind_molecular, _stored_argv
            molecular_spec={'schema':1,'request_id':binding['request_id'],'engine':'ORCA','name':spec['name'],
                'intent':'held','resources':spec['resources'],'workdir':materialized['workdir'],
                'runtime':spec['runtime'],'scientific':materialized['scientific']}
            prepared=bind_molecular(molecular_spec,materialized['prepared_plan'])['plan']
            if (job['argv'],job['cwd'],job['env'])!=(_stored_argv(prepared['argv']),prepared['cwd'],prepared['env']):
                raise JobsError('Successor execution differs from the verified ORCA plan')
        return None
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return 'Successor preflight: '+str(exc)
