"""Selected-attempt read-only review sessions with disposable bounded observers.

Only an explicit start reads scientific output. Preview and cache retrieval never
hash scientific files. Cancellation targets the observer subprocess we created.
"""
from __future__ import annotations
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from .locations import _read_job
from .store import JobsError
from .review_models import DOMAIN_NAMES, ReviewDomain, ReviewRecord


def options(value):
    allowed = {'policy_path','spec_path','max_bytes','timeout_seconds','segment','export_path'}
    if type(value) is not dict or set(value) - allowed:
        raise JobsError('Unsupported review options')
    result = {'max_bytes': 64*1024*1024, 'timeout_seconds': 30, **value}
    for name, maximum in (('max_bytes',2**30), ('timeout_seconds',120)):
        if type(result[name]) is not int or not 1 <= result[name] <= maximum:
            raise JobsError(f'{name} must be an integer from 1 to {maximum}')
    for name in ('policy_path','spec_path','export_path'):
        if name in result and (not isinstance(result[name], str) or not Path(result[name]).is_absolute()
                               or '\0' in result[name]):
            raise JobsError(f'{name} must be an absolute owner-node path')
    if 'segment' in result and (type(result['segment']) is not int or result['segment'] < 0):
        raise JobsError('segment must be a nonnegative integer')
    return result


def source_stat(path):
    from cmw.periodic.vasp.result_sources import _regular_path, _stat
    return _stat(_regular_path(Path(path)))


def source_changed(record):
    for source in json.loads(record.sources_json):
        path = source.get('absolute_path') or source.get('path')
        if any(str(Path(alias).resolve()) != source.get('resolved_path',path) for alias in source.get('declared_aliases', [])):
            return True
        expected = source.get('stat_after') or source.get('stat_before')
        if path and expected:
            try:
                if source_stat(path) != expected:
                    return True
            except (OSError, ValueError):
                return True
    return False


class ReviewService:
    def __init__(self, store, *, origin=None):
        self.store, self.origin = store, dict(origin or {})
        self._tasks = OrderedDict()
        self._requests = {}
        self._lock = threading.RLock()
        self._closed = False
        self._identity = None

    def _job(self, job_id, expected_attempt_id=None):
        job, identity = _read_job(self.store, job_id)
        if self._identity is not None and self._identity != identity:
            raise JobsError('Review queue identity changed')
        self._identity = identity
        if expected_attempt_id is not None and job['attempt_id'] != expected_attempt_id:
            raise JobsError('Review attempt changed')
        enrolled = self.store.queue_identity()
        if enrolled:
            for key in ('node_id','queue_id','host_id'):
                if key in self.origin and self.origin[key] != enrolled[key]:
                    raise JobsError('Review owner identity changed')
        elif self.origin.get('node_id') or self.origin.get('queue_id'):
            raise JobsError('Review requires its enrolled owner queue')
        return job, enrolled or {}

    def _preview(self, job, owner):
        domains = tuple(ReviewDomain(name, 'NOT ASSESSED',
            'Stored Jobs state: ' + job['status'] + '; native receipt not inspected' if name == 'operational'
            else 'Explicit bounded inspection required') for name in DOMAIN_NAMES)
        return ReviewRecord(1, owner.get('node_id'), owner.get('queue_id'), job['id'],job['attempt_id'],
            '', 'PREVIEW', job['engine'], time.time(), domains, 'metadata only', 64*1024*1024,30,
            '[]','{}',json.dumps({'state_directory':str(self.store.root), 'state_identity':self._identity,
                                'origin':self.origin, 'enrollment':owner}),
            'No scientific files read. I inspects; assessment needs an explicit policy.')

    def preview(self, job_id, expected_attempt_id=None):
        with self._lock:
            job, owner = self._job(job_id, expected_attempt_id)
            for task in reversed(self._tasks.values()):
                record = task['record']
                if (record.job_id, record.attempt_id) == (job['id'],job['attempt_id']):
                    if record.status == 'COMPLETE':
                        changed = source_changed(record)
                        domains = tuple(replace(d, status='UNKNOWN') if d.status == 'PASS' else d for d in record.domains)
                        return replace(record, status='STALE' if changed else 'CACHED', domains=domains,
                            message='Source metadata changed; rerun inspection.' if changed else
                            'Historical cached evidence; hashes have not been reverified. Rerun before reuse.')
                    return record
            return self._preview(job, owner)

    def _present(self, task, *, replay=False):
        record = task['record']
        if record.status == 'COMPLETE':
            if source_changed(record):
                return replace(record,status='STALE',
                    domains=tuple(replace(d,status='UNKNOWN') if d.status=='PASS' else d for d in record.domains),
                    message='Source metadata changed; rerun inspection with a new request identity.')
            if replay or task.get('delivered'):
                from .review_models import cached_review
                return cached_review(record)
            task['delivered'] = True
        return record

    def start(self, job_id, supplied, request_id, expected_attempt_id=None):
        settings = options(supplied)
        if not isinstance(request_id,str) or not 1 <= len(request_id) <= 128:
            raise JobsError('Review requires a bounded request identity')
        with self._lock:
            if self._closed:
                raise JobsError('Review session closed')
            job, owner = self._job(job_id, expected_attempt_id)
            key = (job['id'],job['attempt_id'],json.dumps(settings,sort_keys=True))
            if request_id in self._requests:
                old_key, handle = self._requests[request_id]
                if key != old_key:
                    raise JobsError('Review request identity reused with different options/target')
                if handle not in self._tasks:
                    raise JobsError('Review request expired; use a new request identity')
                return self._present(self._tasks[handle],replay=True)
            if any(t['record'].status == 'PENDING' for t in self._tasks.values()):
                raise JobsError('One bounded review is already in flight')
            if len(self._requests) >= 128:
                raise JobsError('Review session request limit reached; open a new session')
            while len(self._tasks) >= 8:
                self._tasks.popitem(last=False)
            handle = uuid4().hex
            record = replace(self._preview(job,owner), review_id=handle,status='PENDING',
                max_bytes=settings['max_bytes'],timeout_seconds=settings['timeout_seconds'],
                message='Owner-node observer starting; cancellation stops only this inspection.')
            task = {'record':record,'settings':settings,'cancel':threading.Event(),'process':None}
            self._tasks[handle] = task
            self._requests[request_id] = (key,handle)
            thread = threading.Thread(target=self._run,args=(task,),daemon=True,name='cmw-result-review')
            task['thread'] = thread
            thread.start()
            return record

    def _run(self, task):
        record = task['record']
        try:
            request = {'state':str(self.store.root),'job_id':record.job_id,'attempt_id':record.attempt_id,
                       'origin':self.origin,'settings':task['settings'],'review_id':record.review_id}
            # Source development honors its explicit PYTHONPATH; installed -I
            # callers keep isolation in the child. No shell or engine is invoked.
            command = [sys.executable, *(['-I'] if sys.flags.isolated else []), '-B', '-m','cmw.jobs.review_worker']
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=output,stderr=errors)
                task['process'] = process
                process.stdin.write(json.dumps(request).encode()); process.stdin.close()
                deadline = time.monotonic() + record.timeout_seconds
                stopped = None
                while process.poll() is None:
                    if task['cancel'].wait(.05):
                        stopped = 'CANCELLED'; break
                    if time.monotonic() >= deadline:
                        stopped = 'LIMIT'; break
                    if output.tell() > 1024*1024 or errors.tell() > 128*1024:
                        stopped = 'LIMIT'; break
                if stopped:
                    process.kill(); process.wait(timeout=5)
                    value = replace(record,status=stopped,message='Observer stopped; scientific execution untouched.')
                else:
                    output.seek(0); raw = output.read(1024*1024+1)
                    if process.returncode or len(raw)>1024*1024:
                        errors.seek(0)
                        raise JobsError('Observer failed: '+errors.read(2048).decode(errors='replace'))
                    value = from_dict(json.loads(raw))
                    self._job(record.job_id,record.attempt_id)
                    if (value.job_id,value.attempt_id,value.node_id,value.queue_id,value.review_id) != (
                            record.job_id,record.attempt_id,record.node_id,record.queue_id,record.review_id):
                        raise JobsError('Observer returned a different target')
            with self._lock:
                task['record'] = value
        except Exception as exc:
            process = task.get('process')
            if process is not None and process.poll() is None:
                process.kill(); process.wait(timeout=5)
            with self._lock:
                task['record'] = replace(record,status='ERROR',message=str(exc)[:2048])

    def _task(self,job_id,handle,expected_attempt_id):
        job,_ = self._job(job_id,expected_attempt_id)
        task = self._tasks.get(handle)
        if task is None or (task['record'].job_id,task['record'].attempt_id) != (job['id'],job['attempt_id']):
            raise JobsError('Unknown, expired or foreign review handle')
        return task

    def poll(self,job_id,review_id,expected_attempt_id=None):
        with self._lock:
            return self._present(self._task(job_id,review_id,expected_attempt_id))

    def cancel(self,job_id,review_id,expected_attempt_id=None):
        with self._lock:
            task = self._task(job_id,review_id,expected_attempt_id)
            task['cancel'].set()
            return replace(task['record'],message='Observer cancellation requested; execution untouched.')

    def close(self):
        with self._lock:
            self._closed = True
            tasks = list(self._tasks.values())
            for task in tasks:
                task['cancel'].set()
        for task in tasks:
            task['thread'].join(timeout=6)


def from_dict(value):
    from dataclasses import fields
    expected = {f.name for f in fields(ReviewRecord)} - {'sources_json','evidence_json','provenance_json'} | {'sources','evidence','provenance'}
    if not isinstance(value,dict) or set(value) != expected:
        raise JobsError('Unsupported review record fields')
    value = dict(value)
    value['domains'] = tuple(ReviewDomain(**d) for d in value['domains'])
    for name in ('sources','evidence','provenance'):
        value[name+'_json'] = json.dumps(value.pop(name),allow_nan=False)
    return ReviewRecord(**value)
