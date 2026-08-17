"""Structured shell bridge for ORCA wavefunction conversion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .conversion import (
    finalize_molden_conversion,
    plan_molden_conversion,
    prepare_molden_conversion,
)


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _plan(args: argparse.Namespace, *, prepare: bool) -> int:
    plan = plan_molden_conversion(
        source_result=args.source,
        output_root=args.output,
        converter=args.converter,
        spin_mode=args.spin_mode,
    )
    _print(prepare_molden_conversion(plan) if prepare else plan.to_dict())
    return 10 if plan.reusable else 0


def _finalize(args: argparse.Namespace) -> int:
    _print(
        finalize_molden_conversion(
            plan_path=args.plan, process_exit_code=args.process_exit_code
        )
    )
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--converter", type=Path, required=True)
    parser.add_argument(
        "--spin-mode", choices=("restricted", "unrestricted"), required=True
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    _common(plan)
    plan.set_defaults(handler=lambda args: _plan(args, prepare=False))
    prepare = sub.add_parser("prepare")
    _common(prepare)
    prepare.set_defaults(handler=lambda args: _plan(args, prepare=True))
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--plan", type=Path, required=True)
    finalize.add_argument("--process-exit-code", type=int, required=True)
    finalize.set_defaults(handler=_finalize)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        _print({"error": type(exc).__name__, "reason": str(exc)})
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
