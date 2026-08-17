"""Shell bridge for dependency-resolved HOF execution plans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .execution import materialize_relaxed_fragment_energy


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _prepare_relaxed(args: argparse.Namespace) -> int:
    result = materialize_relaxed_fragment_energy(
        systems_path=args.systems,
        methods_path=args.methods,
        protocol_path=args.protocol,
        execution_path=args.execution,
        system_id=args.system,
        project_root=args.project_root,
        fragment_id=args.fragment,
        relaxation_result=args.relaxation_result,
        runtime_contract_source=args.runtime_contract,
        cmw_root=args.cmw_root,
        python_bin=args.python,
        orca_executable=args.orca_exe,
    )
    _print(result)
    return 10 if result["status"] == "REUSED" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-relaxed-energy")
    prepare.add_argument("--systems", type=Path, required=True)
    prepare.add_argument("--methods", type=Path, required=True)
    prepare.add_argument("--protocol", type=Path, required=True)
    prepare.add_argument("--execution", type=Path, required=True)
    prepare.add_argument("--system", required=True)
    prepare.add_argument("--project-root", type=Path, required=True)
    prepare.add_argument("--fragment", required=True)
    prepare.add_argument("--relaxation-result", type=Path, required=True)
    prepare.add_argument("--runtime-contract", type=Path, required=True)
    prepare.add_argument("--cmw-root", type=Path, required=True)
    prepare.add_argument("--python", type=Path, required=True)
    prepare.add_argument("--orca-exe", type=Path, required=True)
    prepare.set_defaults(handler=_prepare_relaxed)
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
