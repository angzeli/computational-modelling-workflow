"""Installed CLI for explicit selected-attempt review; owner paths stay owner-local."""
from dataclasses import replace
import json
import time
from uuid import uuid4

from .review import ReviewService, options
from .review_models import ReviewRecord, review_text
from .store import JobsError


def register(operations, handler):
    p = operations.add_parser('review',help='selected-job scientific review; metadata first, inspection explicit')
    p.add_argument('job_id')
    p.add_argument('--json',action='store_true')
    p.add_argument('--inspect',action='store_true',help='explicit bounded full-log/hash observer task')
    p.add_argument('--policy',dest='policy_path',help='explicit owner-node acceptance policy')
    p.add_argument('--spec',dest='spec_path',help='existing owner-node VASP retrospective binding specification')
    p.add_argument('--export',dest='export_path',help='explicit new owner-node record outside calculation inputs; no clobber')
    p.add_argument('--max-bytes',type=int,default=64*1024*1024,help='read limit per source, default 64 MiB; maximum 1 GiB')
    p.add_argument('--timeout-seconds',type=int,default=30,help='observer deadline, 1–120 seconds')
    p.add_argument('--segment',type=int,help='explicit VASP segment index')
    p.set_defaults(handler=handler)


def _settings(args):
    return options({key:getattr(args,key) for key in
        ('policy_path','spec_path','export_path','max_bytes','timeout_seconds','segment')
        if getattr(args,key,None) is not None})


def _requested(args):
    return args.inspect or args.policy_path or args.spec_path or args.export_path


def _print(record,args):
    from .cli import safe_text
    print(json.dumps(record.to_dict(),indent=2,ensure_ascii=True,allow_nan=False) if args.json else safe_text(review_text(record)))
    return 2 if record.status in {'ERROR','LIMIT','CANCELLED','STALE'} else 0


def local(store,args):
    service = ReviewService(store)
    record = None
    try:
        record = service.preview(args.job_id)
        if _requested(args):
            record = service.start(record.job_id,_settings(args),uuid4().hex,record.attempt_id)
            while record.status == 'PENDING':
                time.sleep(.1)
                record = service.poll(record.job_id,record.review_id,record.attempt_id)
        return _print(record,args)
    except KeyboardInterrupt:
        if record and record.review_id:
            service.cancel(record.job_id,record.review_id,record.attempt_id)
        return 130
    finally:
        service.close()


def remote(backend,args):
    from cmw.nodes.codec import decode_typed
    from cmw.nodes.protocol import NodeError
    target = backend.call('lookup',{'id':args.job_id})['result']['target']
    record = decode_typed(backend.call('review.preview',{},target)['result']['review'],ReviewRecord)
    try:
        if _requested(args):
            settings = _settings(args)
            record = decode_typed(backend.call('review.start',{'options':settings},target,
                request_id=uuid4().hex)['result']['review'],ReviewRecord)
            deadline = time.monotonic()+settings['timeout_seconds']+10
            while record.status == 'PENDING' and time.monotonic()<deadline:
                time.sleep(.2)
                record = decode_typed(backend.call('review.poll',{'review_id':record.review_id},target)['result']['review'],ReviewRecord)
        return _print(record,args)
    except NodeError as exc:
        # A broken observation connection supplies no remote execution outcome.
        record = replace(record,message='Connection unavailable; cached/pending observation only: '+str(exc))
        return _print(record,args) or 2
    except KeyboardInterrupt:
        if record.review_id:
            try:
                backend.call('review.cancel',{'review_id':record.review_id},target)
            except NodeError:
                pass
        return 130
