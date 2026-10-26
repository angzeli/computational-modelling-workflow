"""Installed doctor entry with explicit scope and opt-in private export."""
from pathlib import Path
import json


def register(subcommands):
    parser = subcommands.add_parser('doctor', help='read-only runtime/capability diagnosis; no automatic repairs')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--local', action='store_true', help='inspect only this loaded CMW runtime (default)')
    scope.add_argument('--node', help='connect only to this explicitly selected registered node')
    parser.add_argument('--state', type=Path, help='explicit local Jobs state; never initialized')
    parser.add_argument('--queue', help='selected enrolled node queue')
    parser.add_argument('--nodes-config', type=Path)
    parser.add_argument('--job', help='selected job in the explicit state/node')
    from .service import CAPABILITIES
    parser.add_argument('--capability', choices=CAPABILITIES)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--export', type=Path, metavar='NEW_DIRECTORY',
                        help='explicit private no-clobber capsule under Desktop/Scratch; no raw excerpts or uploads')
    parser.set_defaults(handler=handle)


def handle(args):
    from .service import inspect_local, format_report
    if args.node:
        if args.state is not None:
            raise ValueError('--node and --state select different owners')
        from .nodes import inspect_node
        report = inspect_node(args.node, args.queue, nodes_config=args.nodes_config,
                              job_id=args.job, capability=args.capability)
    else:
        if args.queue or args.nodes_config:
            raise ValueError('--queue and --nodes-config require --node')
        report = inspect_local(state=args.state, job_id=args.job, capability=args.capability)
    if args.export:
        from .capsule import export_capsule
        report['export'] = export_capsule(report, args.export)
    print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) if args.json else format_report(report))
    if args.export and not args.json:
        print('Private capsule: ' + str(args.export))
    return 1 if any(item['severity'] == 'ERROR' for item in report['findings']) else 0
