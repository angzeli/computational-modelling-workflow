"""Disposable owner-node observer. stdin is a private parent request, not a job."""
from __future__ import annotations
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

from .review import ReviewService, options, source_changed
from .review_models import DOMAIN_NAMES, ReviewDomain
from .store import Store, JobsError


def _directory(store, job):
    if job.get('molecular_submission'):
        return job['molecular_submission']['spec']['workdir']
    from .progress import resolve_context
    context = resolve_context(store, job['id'], expected_attempt_id=job['attempt_id'])
    if context.binding_status == 'SOURCE_CONFLICT':
        raise JobsError('Scientific source declarations conflict; review cannot choose one')
    paths = {str(Path(s.path).parent) for s in context.sources if s.role.split(':')[0].lower() == 'outcar'}
    if len(paths) > 1:
        raise JobsError('More than one declared scientific output directory')
    # A foreground cwd is an operational declaration, never scientific binding.
    return next(iter(paths)) if paths else job['cwd']


def _export(record, path, forbidden):
    destination = Path(path)
    forbidden = list(forbidden) + [str(Path(source.get('absolute_path') or source['path']).parent)
        for source in json.loads(record.sources_json) if source.get('scientific_input') is True]
    if destination.resolve(strict=False) != destination:
        raise JobsError('Review export cannot traverse symlinks')
    if any(destination.is_relative_to(Path(root).resolve()) for root in forbidden if root):
        raise JobsError('Review export must be outside calculation and Jobs state directories')
    if not destination.parent.is_dir():
        raise JobsError('Review export parent must already exist')
    if source_changed(record):
        raise JobsError('Sources changed before export; review is stale')
    # Recheck complete bytes and original identities with the same bounded reader,
    # never a stat-only acceptance certificate. Partial evidence stays qualified.
    from .review_engines import _Sources
    verifier = _Sources(record.max_bytes, None)
    for expected in json.loads(record.sources_json):
        if expected.get('sha256') and expected.get('coverage') == 'complete':
            actual = verifier.observe(expected.get('absolute_path') or expected['path'])
            if any(actual[key] != expected.get(key) for key in ('sha256','stat_before','bytes_read')):
                raise JobsError('Source reverification failed before export')
    raw = json.dumps(record.to_dict(),indent=2,allow_nan=False).encode()+b'\n'
    fd = os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as out:
        out.write(raw); out.flush(); os.fsync(out.fileno())
    return replace(record,message=record.message+' Exported new owner-node record: '+str(destination))


def inspect(request):
    from cmw.periodic.vasp.result_execution import inspect_execution_snapshot
    from cmw.periodic.vasp.result_sources import read_json_source
    from cmw.nodes.identity import runtime_identity
    from .review_engines import inspect_engine
    settings = options(request['settings'])
    store = Store(request['state'])
    service = ReviewService(store,origin=request['origin'])
    job,owner = service._job(request['job_id'],request['attempt_id'])
    record = replace(service._preview(job,owner),review_id=request['review_id'],
        max_bytes=settings['max_bytes'],timeout_seconds=settings['timeout_seconds'])
    directory = _directory(store,job)
    snapshot = store.snapshot()
    receipt = str(store.root/'attempts'/job['attempt_id']/'payload-exit.json')
    expected = None
    if owner:
        expected = {**{key: owner[key] for key in ('node_id', 'queue_id', 'host_id')},
                    'job_id': job['id'], 'attempt_id': job['attempt_id']}
        # Reviewer runtime and producing runtime are independent identities.
        submitted_runtime = (job.get('node_submission') or {}).get('runtime_id')
        if submitted_runtime:
            expected['runtime_id'] = submitted_runtime
    spec_sources = []
    if settings.get('spec_path'):
        if not settings.get('policy_path'):
            raise JobsError('An explicit policy is required with a finalization specification')
        from cmw.periodic.vasp.result_binding import _validate_spec, _resolved_spec
        spec, source = read_json_source(settings['spec_path'], 'review_spec')
        spec_sources.append(source)
        selected = _resolved_spec(_validate_spec(spec), Path(settings['spec_path']).parent)['execution']
        if (selected['job_id'] != job['id'] or selected['attempt_id'] != job['attempt_id']
                or selected['receipt'] != receipt):
            raise JobsError('Review specification belongs to another job, attempt or original receipt location')
    adapter = inspect_engine(job,directory=directory,policy_path=settings.get('policy_path'),
        spec_path=settings.get('spec_path'),segment=settings.get('segment'),max_bytes=settings['max_bytes'])
    binding = adapter['domains']['source_binding'].get('evidence')
    bound_execution = binding.get('execution') if isinstance(binding, dict) else None
    if bound_execution is not None:
        # The shared finalizer's binding already read the exact runner/native
        # sources, including wrapper/output correspondence. Reuse that decision.
        selected_job = bound_execution.get('job')
        association = bound_execution.get('binding') or {}
        if selected_job and (selected_job['id'] != job['id'] or selected_job['attempt_id'] != job['attempt_id']):
            raise JobsError('Bound result selects a different operational attempt')
        if association and (association.get('state_directory') != str(store.root)
                            or association.get('expected_receipt') != receipt):
            raise JobsError('Bound result belongs to a different original queue or receipt')
        enrolled = association.get('queue_identity')
        if enrolled is not None and enrolled != owner:
            raise JobsError('Bound result belongs to a different enrolled owner')
        execution = bound_execution
    else:
        execution = inspect_execution_snapshot(snapshot,receipt,job_id=job['id'],attempt_id=job['attempt_id'],
            run_directory=directory,expected_target=expected)
    domain_values = adapter['domains']
    operation_status = 'PASS' if execution['operational_success'] is True else 'FAIL' if execution['operational_success'] is False else 'UNKNOWN'
    domain_values['operational'] = {'status':operation_status,
        'summary':'Original native foreground completion evidence; not scientific convergence' if operation_status == 'PASS'
        else '; '.join(f['message'] for f in execution['findings'])[:4096]}
    if domain_values['policy']['status'] == 'PASS' and operation_status != 'PASS':
        domain_values['policy'] = {'status':'UNKNOWN','summary':'Policy cannot establish selected producer validity: native operational evidence is not eligible'}
    sources = [*execution.get('sources',[]), *adapter.get('sources',[]), *spec_sources]
    seen = set()
    sources = [s for s in sources if not (str(s.get('absolute_path',s.get('path'))) in seen or seen.add(str(s.get('absolute_path',s.get('path')))))]
    provenance = json.loads(record.provenance_json)
    provenance.update({'runtime':runtime_identity(),'policy':adapter.get('policy_identity'),
                       'directory':directory,'snapshot_identity':execution.get('snapshot_identity'),
                       'review_options':settings,'assessed_at':time.time(),
                       'interpretation':'Retrospective observation; not a finalized artifact or permanent certificate'})
    evidence = {'engine':adapter.get('evidence',{}),'execution':execution,
                'domains': {key: value.get('evidence') for key, value in domain_values.items()},
                'limitations':adapter.get('limitations',[])}
    domains = tuple(ReviewDomain(name,
        'N/A' if domain_values[name]['status'] in {'NOT APPLICABLE', 'NOT_APPLICABLE'} else domain_values[name]['status'],
        domain_values[name]['summary'][:4096]) for name in DOMAIN_NAMES)
    coverage = adapter.get('coverage','unknown')
    if not isinstance(coverage,str):
        coverage = json.dumps(coverage,sort_keys=True)
    record = replace(record,status='COMPLETE',observed_at=time.time(),domains=domains,coverage=coverage,
        sources_json=json.dumps(sources,allow_nan=False),evidence_json=json.dumps(evidence,allow_nan=False),
        provenance_json=json.dumps(provenance,allow_nan=False),
        message='Completed bounded owner-node review. Each domain has its own scope; no overall scientific PASS.')
    if source_changed(record):
        record = replace(record,status='STALE',domains=tuple(replace(d,status='UNKNOWN') if d.status=='PASS' else d for d in record.domains),
                         message='Sources changed during review; acceptance is not established.')
    service._job(job['id'],job['attempt_id'])
    if settings.get('export_path'):
        if record.status == 'STALE':
            raise JobsError('Stale review cannot be exported for reuse')
        forbidden = [directory,job['cwd'],str(store.root)]
        review_metadata = {'policy','review_policy','finalization_spec','review_spec','jobs_snapshot','jobs_receipt'}
        for source in sources:
            role = source.get('role','')
            if role not in review_metadata and role != 'prepared_dependency':
                path = source.get('absolute_path') or source.get('path')
                if path:
                    forbidden.append(str(Path(path).parent))
        if job.get('molecular_submission'):
            forbidden.append(job['molecular_submission']['spec']['workdir'])
        record = _export(record,settings['export_path'],forbidden)
    return record


def main():
    request = json.loads(sys.stdin.buffer.read(1024*1024))
    print(json.dumps(inspect(request).to_dict(),allow_nan=False))

if __name__ == '__main__':
    main()
