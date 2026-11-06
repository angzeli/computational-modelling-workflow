"""Explicit owner-local successor planning, publication and held registration."""
from __future__ import annotations

import json

from .store import JobsError
from .successor_models import SuccessorUnverifiable

OPERATIONS = {'plan-next': 'plan', 'materialize-next': 'materialize', 'register-next': 'register'}


def register(operations, handler):
    descriptions = {
        'plan-next': 'read-only reviewed successor plan; never prepares or schedules work',
        'materialize-next': 'publish the exact approved successor into a new owner-local destination',
        'register-next': 'register one prepared successor on Hold; never release or start it',
    }
    for name, description in descriptions.items():
        command = operations.add_parser(name, help=description)
        command.add_argument('job_id', help='exact parent job; the specification must bind its immutable attempt')
        if name == 'register-next':
            command.add_argument('--materialization', required=True, help='published materialization record on the owning node')
            command.add_argument('--request-id', required=True, help='durable UUID hex; reuse with identical parameters after a lost response')
        else:
            command.add_argument('--spec', required=True, help='caller-owned successor specification on the owning node')
        if name != 'plan-next':
            command.add_argument('--approve-digest', required=True, help='content-bound digest from the reviewed plan')
        command.add_argument('--expected-attempt-id', help='refuse if the selected parent immutable attempt differs')
        command.add_argument('--json', action='store_true')
        command.set_defaults(handler=handler)


def _print(result, args):
    from .cli import safe_text
    value = result.to_dict()
    if result.retry_code:
        value.update(code=result.retry_code, exit_code=75)
    # The complete plan remains visible in text mode; no truncated summary is approval.
    text = json.dumps(value, indent=2, ensure_ascii=args.json, allow_nan=False)
    print(text if args.json else safe_text(text))
    if result.retry_code:
        return 75
    return 2 if result.status == 'BLOCKED' else 0


def local(store, args):
    from .successor import SuccessorService
    service = SuccessorService(store)
    expected = {'expected_attempt_id': args.expected_attempt_id}
    try:
        if args.jobs_operation == 'plan-next':
            result = service.plan(args.job_id, args.spec, **expected)
        elif args.jobs_operation == 'materialize-next':
            result = service.materialize(args.job_id, args.spec, args.approve_digest, **expected)
        else:
            result = service.register(args.job_id, args.materialization, args.approve_digest,
                                      args.request_id, **expected)
    except SuccessorUnverifiable as exc:
        return print_retry(exc.code, str(exc), args)
    except ValueError as exc:
        raise JobsError(str(exc)) from exc
    return _print(result, args)


def print_retry(code, reason, args):
    from .cli import safe_text
    status = 'PENDING_VERIFICATION' if code == 'SUCCESSOR_PENDING_VERIFICATION' else 'UNVERIFIABLE'
    value = {'status': status, 'code': code, 'reason': reason, 'exit_code': 75}
    print(json.dumps(value, ensure_ascii=True) if args.json else safe_text(f'{status}: {reason}'))
    return 75


def remote(backend, args):
    from cmw.nodes.codec import decode_typed
    from cmw.nodes.protocol import NodeError
    from .successor_models import SuccessorResult
    target = backend.call('lookup', {'id': args.job_id})['result']['target']
    if args.expected_attempt_id not in (None, target['attempt_id']):
        raise NodeError('ATTEMPT_CHANGED', 'Selected parent differs from the explicitly expected immutable attempt')
    action = OPERATIONS[args.jobs_operation]
    params = ({'record_path': args.materialization} if action == 'register' else {'spec_path': args.spec})
    if action != 'plan':
        params['approved_digest'] = args.approve_digest
    response = backend.call('successor.' + action, params, target,
                            request_id=getattr(args, 'request_id', None))
    return _print(decode_typed(response['result']['successor'], SuccessorResult), args)
