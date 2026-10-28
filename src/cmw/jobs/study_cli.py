"""Explicit study planning, publication, held registration and qualified comparison."""
from __future__ import annotations

import json
from .store import JobsError


def register(operations, handler):
    command = operations.add_parser('study', help='explicit reviewed VASP convergence studies; never schedule a sweep')
    commands = command.add_subparsers(dest='study_operation', required=True)
    descriptions = {
        'plan': 'validate the complete caller-declared matrix without writes',
        'materialize': 'publish the exact approved cases to new owner-local locations',
        'register': 'register prepared cases on Hold with durable per-case receipts',
        'compare': 'compare explicit selected attempt evidence under the declared rule',
        'view': 'show bounded recorded case bindings; no scientific inspection',
    }
    for action, description in descriptions.items():
        sub = commands.add_parser(action, help=description)
        if action in {'plan', 'materialize'}:
            sub.add_argument('--manifest', required=True, help='absolute caller-owned manifest on the owner node')
        else:
            sub.add_argument('--record', required=True, help='absolute study record on the owner node')
        if action in {'materialize', 'register'}:
            sub.add_argument('--approve-digest', required=True, help='exact reviewed content-bound study digest')
        if action == 'register':
            sub.add_argument('--request-id', required=True, help='manifest study UUID hex; reuse after a lost response')
        if action == 'compare':
            sub.add_argument('--selection', required=True, help='owner-local exact result-attempt selections; never inferred from names')
            sub.add_argument('--export', help='explicit new owner-local directory for JSON, CSV and Markdown')
        sub.add_argument('--json', action='store_true')
        sub.set_defaults(handler=handler)


def _print(result, args):
    from .cli import safe_text
    if args.study_operation == 'view' and not args.json:
        # Keep the core CLI independent of optional Textual imports.
        from .study_presentation import study_text
        print(study_text(result))
    else:
        text = json.dumps(result.to_dict(), indent=2, ensure_ascii=args.json, allow_nan=False)
        print(text if args.json else safe_text(text))
    return 2 if result.status in {'BLOCKED', 'PARTIAL', 'NOT_BOUND'} else 0


def local(store, args):
    from .study import StudyService
    service = StudyService(store)
    try:
        action = args.study_operation
        if action == 'plan':
            result = service.plan(args.manifest)
        elif action == 'materialize':
            result = service.materialize(args.manifest, args.approve_digest)
        elif action == 'register':
            result = service.register(args.record, args.approve_digest, args.request_id)
        elif action == 'compare':
            result = service.compare(args.record, args.selection, export_directory=args.export)
        else:
            result = service.view(args.record)
    except ValueError as exc:
        raise JobsError(str(exc)) from exc
    return _print(result, args)


def remote(backend, args):
    from cmw.nodes.codec import decode_typed
    from .study_models import StudyResult
    action = args.study_operation
    params = {'manifest_path': args.manifest} if action in {'plan', 'materialize'} else {'record_path': args.record}
    if action in {'materialize', 'register'}:
        params['approved_digest'] = args.approve_digest
    if action == 'compare':
        params['selection_path'] = args.selection
        if args.export is not None:
            params['export_directory'] = args.export
    result = backend.call('study.'+action, params, request_id=getattr(args, 'request_id', None))
    return _print(decode_typed(result['result']['study'], StudyResult), args)
