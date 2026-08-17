"""Shell bridge for dependency-resolved HOF execution plans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .execution import (
    materialize_hof_orca_node,
    materialize_relaxed_fragment_energy,
    promote_hof_geometry,
)


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


def _prepare_node(args: argparse.Namespace) -> int:
    result = materialize_hof_orca_node(
        systems_path=args.systems,
        methods_path=args.methods,
        protocol_path=args.protocol,
        execution_path=args.execution,
        system_id=args.system,
        project_root=args.project_root,
        node_id=args.node,
        runtime_contract_source=args.runtime_contract,
        cmw_root=args.cmw_root,
        python_bin=args.python,
        orca_executable=args.orca_exe,
        optimization_result=args.optimization_result,
        frequency_result=args.frequency_result,
        geometry_result=args.geometry_result,
    )
    _print(result)
    return 10 if result["status"] == "REUSED" else 0


def _promote_geometry(args: argparse.Namespace) -> int:
    _print(
        promote_hof_geometry(
            optimization_result=args.optimization_result,
            frequency_result=args.frequency_result,
            system_id=args.system,
            output_path=args.output,
        )
    )
    return 0


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
    node = sub.add_parser("prepare-node")
    node.add_argument("--systems", type=Path, required=True)
    node.add_argument("--methods", type=Path, required=True)
    node.add_argument("--protocol", type=Path, required=True)
    node.add_argument("--execution", type=Path, required=True)
    node.add_argument("--system", required=True)
    node.add_argument("--project-root", type=Path, required=True)
    node.add_argument("--node", required=True)
    node.add_argument("--runtime-contract", type=Path, required=True)
    node.add_argument("--cmw-root", type=Path, required=True)
    node.add_argument("--python", type=Path, required=True)
    node.add_argument("--orca-exe", type=Path, required=True)
    node.add_argument("--optimization-result", type=Path)
    node.add_argument("--frequency-result", type=Path)
    node.add_argument("--geometry-result", type=Path)
    node.set_defaults(handler=_prepare_node)
    promote = sub.add_parser("promote-geometry")
    promote.add_argument("--optimization-result", type=Path, required=True)
    promote.add_argument("--frequency-result", type=Path, required=True)
    promote.add_argument("--system", required=True)
    promote.add_argument("--output", type=Path, required=True)
    promote.set_defaults(handler=_promote_geometry)
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
