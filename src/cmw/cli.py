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
