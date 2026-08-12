"""Structured shell bridge for the selectable OPT / FREQ / SP workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cmw.molecular.orca.status import StageType

from .opt_freq_sp import (
    advance_state,
    build_state,
    human_plan,
    initialize_state,
    next_action,
    plan_summary,
)


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--mode", required=True)
    subparser.add_argument("--structure", type=Path, required=True)
    subparser.add_argument("--config", type=Path, required=True)
    subparser.add_argument("--output", type=Path)
    subparser.add_argument("--orca-exe")
    subparser.add_argument("--nprocs", type=int)
    subparser.add_argument("--maxcore", type=int)
    minimum = subparser.add_mutually_exclusive_group()
    minimum.add_argument("--require-minimum", action="store_true", default=None)
    minimum.add_argument("--no-require-minimum", action="store_false", dest="require_minimum")
    subparser.add_argument("--imaginary-tolerance", type=float)


def _build(args: argparse.Namespace) -> dict[str, object]:
    return build_state(
        mode=args.mode,
        structure_path=args.structure,
        config_path=args.config,
        output_override=args.output,
        orca_exe=args.orca_exe,
        nprocs=args.nprocs,
        maxcore_mb=args.maxcore,
        require_minimum=args.require_minimum,
        imaginary_tolerance_cm1=args.imaginary_tolerance,
    )


def _plan(args: argparse.Namespace) -> int:
    state = plan_summary(_build(args))
    if args.format == "json":
        _print(state)
    else:
        print(human_plan(state))
    return 0


def _init(args: argparse.Namespace) -> int:
    state = _build(args)
    state_path = (
        Path(state["output_root"])
        / "workflows"
        / str(state["workflow_target_id"])
        / "workflow_state.json"
    )
    initialize_state(state_path, state, restart_incomplete=args.new_attempt)
    _print({"state_path": str(state_path), "result_path": state["result_path"]})
    return 0


def _next(args: argparse.Namespace) -> int:
    action, payload = next_action(args.state)
    _print({"action": action, **payload})
    return {"RUN": 0, "COMPLETE": 10, "FAILED": 20}[action]


def _advance(args: argparse.Namespace) -> int:
    valid, decision = advance_state(args.state, stage=StageType(args.stage))
    _print({"valid": valid, **decision})
    return 0 if valid else 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    _common(plan)
    plan.add_argument("--format", choices=("human", "json"), default="human")
    plan.set_defaults(handler=_plan)
    init = sub.add_parser("init")
    _common(init)
    init.add_argument("--new-attempt", action="store_true")
    init.set_defaults(handler=_init)
    next_parser = sub.add_parser("next")
    next_parser.add_argument("--state", type=Path, required=True)
    next_parser.set_defaults(handler=_next)
    advance = sub.add_parser("advance")
    advance.add_argument("--state", type=Path, required=True)
    advance.add_argument("--stage", choices=[stage.value for stage in StageType], required=True)
    advance.set_defaults(handler=_advance)
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
