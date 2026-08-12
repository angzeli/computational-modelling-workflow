"""Machine-readable shell bridge for FMO and ESP Multiwfn workflows."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cmw.core.locks import acquire_lock, release_lock
from cmw.molecular.multiwfn.runtime import resolve_threads

from .multiwfn_analysis import (
    AnalysisOperation,
    finalize_analysis,
    normalize_outputs,
    plan_analysis,
    prepare_analysis,
    write_menu,
)


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _plan(args: argparse.Namespace) -> int:
    plan = plan_analysis(
        operation=AnalysisOperation(args.operation),
        source_path=args.source,
        output_root=args.output,
        grid_spacing_bohr=args.grid_spacing_bohr,
        threads=resolve_threads(cli_value=args.threads),
        fragment_path=args.fragments,
        igmh_config_path=args.config,
    )
    _print(plan)
    return 0


def _prepare(args: argparse.Namespace) -> int:
    plan = plan_analysis(
        operation=AnalysisOperation(args.operation),
        source_path=args.source,
        output_root=args.output,
        grid_spacing_bohr=args.grid_spacing_bohr,
        threads=resolve_threads(cli_value=args.threads),
        fragment_path=args.fragments,
        igmh_config_path=args.config,
    )
    if plan["reuse"]["reuse"]:
        _print(plan)
        return 10
    _print(prepare_analysis(plan))
    return 0


def _menu(args: argparse.Namespace) -> int:
    _print(
        write_menu(
            target_path=args.target,
            runtime_path=args.runtime,
            destination=args.destination,
        )
    )
    return 0


def _finalize(args: argparse.Namespace) -> int:
    record = finalize_analysis(
        target_path=args.target,
        runtime_path=args.runtime,
        menu_path=args.menu,
        attempt_directory=args.attempt_directory,
        process_exit_code=args.process_exit_code,
        repository=args.repository,
    )
    _print(record)
    return 0


def _normalize(args: argparse.Namespace) -> int:
    _print(
        normalize_outputs(
            target_path=args.target, attempt_directory=args.attempt_directory
        )
    )
    return 0


def _lock(args: argparse.Namespace) -> int:
    if args.lock_action == "acquire":
        owner = acquire_lock(args.lock, job_id=args.job_id, owner_pid=args.owner_pid)
        _print({"acquired": True, "owner": owner.__dict__})
        return 0
    released = release_lock(args.lock, token=args.token)
    _print({"released": released})
    return 0 if released else 5


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operation", choices=[item.value for item in AnalysisOperation], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--grid-spacing-bohr", type=float)
    parser.add_argument("--fragments", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--threads")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    _common(plan)
    plan.set_defaults(handler=_plan)
    prepare = sub.add_parser("prepare")
    _common(prepare)
    prepare.set_defaults(handler=_prepare)
    menu = sub.add_parser("menu")
    menu.add_argument("--target", type=Path, required=True)
    menu.add_argument("--runtime", type=Path, required=True)
    menu.add_argument("--destination", type=Path, required=True)
    menu.set_defaults(handler=_menu)
    normalize = sub.add_parser("normalize")
    normalize.add_argument("--target", type=Path, required=True)
    normalize.add_argument("--attempt-directory", type=Path, required=True)
    normalize.set_defaults(handler=_normalize)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--target", type=Path, required=True)
    finalize.add_argument("--runtime", type=Path, required=True)
    finalize.add_argument("--menu", type=Path, required=True)
    finalize.add_argument("--attempt-directory", type=Path, required=True)
    finalize.add_argument("--process-exit-code", type=int, required=True)
    finalize.add_argument("--repository", type=Path)
    finalize.set_defaults(handler=_finalize)
    lock = sub.add_parser("lock")
    lock_sub = lock.add_subparsers(dest="lock_action", required=True)
    acquire = lock_sub.add_parser("acquire")
    acquire.add_argument("--lock", type=Path, required=True)
    acquire.add_argument("--job-id", required=True)
    acquire.add_argument("--owner-pid", type=int, default=os.getpid())
    acquire.set_defaults(handler=_lock)
    release = lock_sub.add_parser("release")
    release.add_argument("--lock", type=Path, required=True)
    release.add_argument("--token", required=True)
    release.set_defaults(handler=_lock)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        _print({"error": type(exc).__name__, "reason": str(exc)})
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
