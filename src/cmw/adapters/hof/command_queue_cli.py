"""Generate or verify a HOF queue without launching scientific software."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from .command_queue import (
    authorize_hof_command_queue,
    generate_hof_command_queue,
    verify_hof_command_queue,
)


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _generate(args: argparse.Namespace) -> int:
    _print(
        generate_hof_command_queue(
            preflight_directory=args.preflight,
            output_directory=args.output,
            cmw_root=args.cmw_root,
            hof_root=args.hof_root,
            python_bin=args.python,
            orca_executable=args.orca_exe,
            converter=args.converter,
            multiwfn_executable=args.multiwfn_exe,
            multiwfn_settings=args.settings,
        )
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    _print(
        verify_hof_command_queue(
            queue_path=args.queue,
            authorization_path=args.authorization,
            require_authorized=args.require_authorized,
        )
    )
    return 0


def _authorize(args: argparse.Namespace) -> int:
    _print(
        authorize_hof_command_queue(
            queue_path=args.queue,
            authorization_path=args.authorization,
            report_path=args.report,
            concurrent_scientific_jobs=args.concurrent_scientific_jobs,
            conflicting_locks=args.conflicting_locks,
            package_validation=args.package_validation,
            resource_validation=args.resource_validation,
            command_validation=args.command_validation,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--preflight", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--cmw-root", type=Path, required=True)
    generate.add_argument("--hof-root", type=Path, required=True)
    generate.add_argument("--python", type=Path, required=True)
    generate.add_argument("--orca-exe", type=Path, required=True)
    generate.add_argument("--converter", type=Path, required=True)
    generate.add_argument("--multiwfn-exe", type=Path, required=True)
    generate.add_argument("--settings", type=Path, required=True)
    generate.set_defaults(handler=_generate)
    verify = sub.add_parser("verify")
    verify.add_argument("--queue", type=Path, required=True)
    verify.add_argument("--authorization", type=Path, required=True)
    verify.add_argument("--require-authorized", action="store_true")
    verify.set_defaults(handler=_verify)
    authorize = sub.add_parser("authorize")
    authorize.add_argument("--queue", type=Path, required=True)
    authorize.add_argument("--authorization", type=Path, required=True)
    authorize.add_argument("--report", type=Path, required=True)
    authorize.add_argument("--concurrent-scientific-jobs", required=True)
    authorize.add_argument("--conflicting-locks", required=True)
    authorize.add_argument("--package-validation", required=True)
    authorize.add_argument("--resource-validation", required=True)
    authorize.add_argument("--command-validation", required=True)
    authorize.set_defaults(handler=_authorize)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as exc:
        _print({"error": type(exc).__name__, "reason": str(exc)})
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
