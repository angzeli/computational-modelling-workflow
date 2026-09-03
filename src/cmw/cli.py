"""Canonical command-line interface for CMW platform operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from cmw.core.attempt_cleanup import (
    CLEANUP_TOOL_VERSION,
    DEFAULT_DIAGNOSTIC_TAIL_BYTES,
    DEFAULT_TOMBSTONE_MAX_BYTES,
    CleanupError,
    CleanupExitCode,
    CleanupMode,
    apply_cleanup_plan,
    build_cleanup_plan,
    cleanup_exit_code,
    format_cleanup_plan,
)
from cmw.core.execution_layout import (
    ExecutionLayoutVersion,
    format_layout_status,
    resolve_recorded_layout,
)
from cmw.core.execution_layout_migration import (
    LayoutMigrationError,
    apply_migration_plan,
    audit_migration,
    build_migration_plan,
    format_migration_plan,
    resume_migration,
    rollback_migration,
)
from cmw.core.provenance import atomic_write_json


def _cleanup_attempts(args: argparse.Namespace) -> int:
    if not args.superseded_only:
        raise CleanupError(
            "SUPERSEDED_ONLY_REQUIRED",
            "v1 cleanup requires --superseded-only; canonical/current deletion is unsupported",
            CleanupExitCode.INVALID_ARGUMENT,
        )
    plan = build_cleanup_plan(
        args.campaign,
        mode=args.mode,
        reason=args.reason,
        tombstone_max_bytes=args.tombstone_max_bytes,
        diagnostic_tail_bytes=args.diagnostic_tail_bytes,
    )
    if not args.apply:
        payload = {"operation": "DRY_RUN", "mutated": False, "plan": plan.to_dict()}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else format_cleanup_plan(plan))
        return int(cleanup_exit_code(plan))
    result = apply_cleanup_plan(
        plan,
        confirm_plan=args.confirm_plan,
        reason=args.reason,
        tombstone_max_bytes=args.tombstone_max_bytes,
        diagnostic_tail_bytes=args.diagnostic_tail_bytes,
    )
    payload = {"operation": "APPLY", "plan": plan.to_dict(), "result": result.to_dict()}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_cleanup_plan(plan))
        print(
            f"applied: {result.status}; transaction: {result.transaction_id or '-'}; "
            f"cleaned: {result.attempts_cleaned}; removed: {result.bytes_removed} bytes"
        )
    return int(cleanup_exit_code(plan, result))


def _migrate_execution_layout(args: argparse.Namespace) -> int:
    if args.resume or args.rollback or args.audit:
        if not args.transaction:
            raise LayoutMigrationError(
                "TRANSACTION_REQUIRED",
                "--resume/--rollback/--audit requires --transaction",
                2,
            )
        if not args.audit and args.evidence_dir is None:
            raise LayoutMigrationError(
                "EVIDENCE_DIRECTORY_REQUIRED",
                "recovery requires --evidence-dir",
                2,
            )
        if args.audit:
            result = audit_migration(
                args.campaign, transaction_id=args.transaction
            )
        else:
            result = (
                resume_migration(
                    args.campaign,
                    transaction_id=args.transaction,
                    evidence_directory=args.evidence_dir,
                )
                if args.resume
                else rollback_migration(
                    args.campaign,
                    transaction_id=args.transaction,
                    evidence_directory=args.evidence_dir,
                )
            )
        print(json.dumps(result, indent=2, sort_keys=True) if args.json else f"migration: {result['status']}")
        return 0
    plan = build_migration_plan(
        args.campaign,
        source_version=ExecutionLayoutVersion(args.source_version),
        destination_version=ExecutionLayoutVersion(args.destination_version),
        short_id_width=args.short_id_width,
        include_roots=args.include_root,
    )
    if args.plan_output is not None:
        atomic_write_json(args.plan_output.expanduser().resolve(), plan.to_dict())
    if not args.apply:
        payload = {"operation": "DRY_RUN", "mutated": False, "plan": plan.to_dict()}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else format_migration_plan(plan))
        return 11 if plan.blockers else 0
    if not args.confirm_plan:
        raise LayoutMigrationError(
            "CONFIRM_PLAN_REQUIRED",
            "--apply requires the exact dry-run --confirm-plan SHA-256",
            2,
        )
    if args.evidence_dir is None:
        raise LayoutMigrationError(
            "EVIDENCE_DIRECTORY_REQUIRED",
            "--apply requires --evidence-dir outside the campaign",
            2,
        )
    result = apply_migration_plan(
        plan,
        confirm_plan=args.confirm_plan,
        evidence_directory=args.evidence_dir,
    )
    payload = {"operation": "APPLY", "plan": plan.to_dict(), "result": dict(result)}
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else f"migration: {result['status']}")
    return 0


def _execution_layout_status(args: argparse.Namespace) -> int:
    layout_path = args.layout.expanduser().resolve(strict=True)
    layout = resolve_recorded_layout(
        json.loads(layout_path.read_text(encoding="utf-8")),
        metadata_path=layout_path,
    )
    if args.json:
        payload = {
            "layout_version": layout.version.value,
            "system": layout.system_identifier,
            "stage": layout.workflow_node_identifier,
            "target": layout.display_target_identifier,
            "attempt": layout.attempt_identifier,
            "path": str(layout.working_directory),
        }
        if args.verbose:
            payload["full_target_id"] = layout.target_identifier
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_layout_status(layout, verbose=args.verbose))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cmw", description=__doc__)
    parser.add_argument("--version", action="version", version=CLEANUP_TOOL_VERSION)
    subcommands = parser.add_subparsers(dest="command", required=True)
    cleanup = subcommands.add_parser(
        "cleanup-attempts",
        help="dry-run or apply crash-safe cleanup of provably superseded attempts",
        description=(
            "Build a deterministic cleanup plan first. The default is a read-only "
            "minimal-provenance dry run; purge is irreversible and requires the exact plan hash."
        ),
        epilog=(
            "Examples:\n"
            "  cmw cleanup-attempts --campaign /path/to/campaign --superseded-only\n"
            "  cmw cleanup-attempts --campaign /path/to/campaign --superseded-only "
            "--mode minimal-provenance --apply\n"
            "  cmw cleanup-attempts --campaign /path/to/campaign --superseded-only "
            "--mode purge --apply --confirm-plan FULL_SHA256"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cleanup.add_argument("--campaign", type=Path, required=True, help="campaign path or registered campaign id")
    cleanup.add_argument(
        "--superseded-only",
        action="store_true",
        help="required v1 safety scope; never select canonical/current attempts",
    )
    cleanup.add_argument(
        "--mode",
        choices=tuple(item.value for item in CleanupMode),
        default=CleanupMode.MINIMAL_PROVENANCE.value,
    )
    cleanup.add_argument("--apply", action="store_true", help="apply the displayed fresh plan")
    cleanup.add_argument("--confirm-plan", help="exact 64-character plan SHA-256 required for purge")
    cleanup.add_argument("--reason", help="operator-supplied cleanup reason")
    cleanup.add_argument("--json", action="store_true", help="emit one machine-readable JSON document")
    cleanup.add_argument(
        "--tombstone-max-bytes",
        type=int,
        default=DEFAULT_TOMBSTONE_MAX_BYTES,
        help=f"maximum minimal-provenance tombstone size (default: {DEFAULT_TOMBSTONE_MAX_BYTES})",
    )
    cleanup.add_argument(
        "--diagnostic-tail-bytes",
        type=int,
        default=DEFAULT_DIAGNOSTIC_TAIL_BYTES,
        help=f"maximum retained diagnostic tail (default: {DEFAULT_DIAGNOSTIC_TAIL_BYTES})",
    )
    cleanup.set_defaults(handler=_cleanup_attempts)
    migration = subcommands.add_parser(
        "migrate-execution-layout",
        help="plan or apply a crash-safe execution-layout migration",
        description=(
            "Build a deterministic v1-to-v2 plan. Dry run is the default; apply "
            "requires its exact plan hash and an external evidence directory."
        ),
    )
    migration.add_argument("--campaign", type=Path, required=True)
    migration.add_argument(
        "--from",
        dest="source_version",
        choices=("v1",),
        default="v1",
    )
    migration.add_argument(
        "--to",
        dest="destination_version",
        choices=("v2",),
        default="v2",
    )
    migration.add_argument("--short-id-width", type=int, default=12)
    migration.add_argument(
        "--include-root",
        type=Path,
        action="append",
        default=[],
        help="migrate only v1 targets whose recorded scientific leaf is under this root",
    )
    migration.add_argument("--apply", action="store_true")
    migration.add_argument("--confirm-plan")
    migration.add_argument("--evidence-dir", type=Path)
    migration.add_argument("--plan-output", type=Path)
    recovery = migration.add_mutually_exclusive_group()
    recovery.add_argument("--resume", action="store_true")
    recovery.add_argument("--rollback", action="store_true")
    recovery.add_argument("--audit", action="store_true")
    migration.add_argument("--transaction")
    migration.add_argument("--json", action="store_true")
    migration.set_defaults(handler=_migrate_execution_layout)
    layout_status = subcommands.add_parser(
        "execution-layout-status",
        help="show a readable v1/v2 attempt identity",
    )
    layout_status.add_argument("--layout", type=Path, required=True)
    layout_status.add_argument("--verbose", action="store_true")
    layout_status.add_argument("--json", action="store_true")
    layout_status.set_defaults(handler=_execution_layout_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except CleanupError as exc:
        json_requested = bool("--json" in (argv if argv is not None else sys.argv[1:]))
        payload = {"status": "FAILED", "code": exc.code, "reason": str(exc), "exit_code": int(exc.exit_code)}
        if json_requested:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"cmw cleanup-attempts: {exc.code}: {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except LayoutMigrationError as exc:
        json_requested = bool("--json" in (argv if argv is not None else sys.argv[1:]))
        payload = {
            "status": "FAILED",
            "code": exc.code,
            "reason": str(exc),
            "exit_code": int(exc.exit_code),
        }
        if json_requested:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"cmw migrate-execution-layout: {exc.code}: {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        payload = {
            "status": "FAILED",
            "code": "INVALID_ARGUMENT",
            "reason": str(exc),
            "exit_code": int(CleanupExitCode.INVALID_ARGUMENT),
        }
        json_requested = bool("--json" in (argv if argv is not None else sys.argv[1:]))
        if json_requested:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"cmw: INVALID_ARGUMENT: {exc}", file=sys.stderr)
        return int(CleanupExitCode.INVALID_ARGUMENT)


if __name__ == "__main__":
    raise SystemExit(main())
