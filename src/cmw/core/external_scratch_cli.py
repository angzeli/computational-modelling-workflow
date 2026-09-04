"""Machine-readable CLI for operational external scratch transactions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from .execution_layout import resolve_recorded_layout
from .external_scratch import (
    EXTERNAL_SCRATCH_RECORD,
    ExternalScratchError,
    cleanup_external_scratch,
    copy_back_external_scratch,
    mark_external_scratch_failed,
    mark_external_scratch_running,
    prepare_external_scratch,
)
from .provenance import read_json


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _mapping(values: list[str], *, name: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        role, separator, path = value.partition("=")
        if not separator or not role or not path or role in result:
            raise ExternalScratchError(f"invalid {name} declaration: {value!r}")
        result[role] = Path(path)
    return result


def _target_id(path: Path) -> str:
    value = read_json(path)
    target = value.get("target")
    if not isinstance(target, Mapping) or not target.get("target_id"):
        raise ExternalScratchError("target record lacks a scientific target identity")
    return str(target["target_id"])


def _prepare(args: argparse.Namespace) -> int:
    layout_path = Path(args.layout)
    layout = resolve_recorded_layout(
        read_json(layout_path), metadata_path=layout_path
    )
    attempt = layout.working_directory
    primary = Path(args.input)
    additional_inputs = _mapping(args.execution_input, name="input")
    if "primary" in additional_inputs:
        raise ExternalScratchError("input role 'primary' is reserved")
    inputs = {"primary": primary, **additional_inputs}
    if args.geometry_contract:
        geometry = read_json(Path(args.geometry_contract)).get("input_geometry_file")
        if not geometry:
            raise ExternalScratchError("geometry contract lacks its input path")
        geometry_path = Path(str(geometry))
        existing = inputs.get("geometry")
        if existing is not None and existing.resolve() != geometry_path.resolve():
            raise ExternalScratchError("geometry input declarations conflict")
        inputs["geometry"] = geometry_path
    additional_outputs = _mapping(args.artifact, name="output")
    reserved_outputs = {"output", "stderr"}.intersection(additional_outputs)
    if reserved_outputs:
        raise ExternalScratchError(
            "output roles are reserved: " + ", ".join(sorted(reserved_outputs))
        )
    outputs = {
        "output": Path(args.output),
        "stderr": Path(args.stderr),
        **additional_outputs,
    }
    record = prepare_external_scratch(
        scratch_root=Path(args.scratch_root),
        mount_path=Path(args.mount),
        canonical_attempt_directory=attempt,
        target_id=_target_id(Path(args.target)),
        attempt_id=layout.attempt_identifier,
        input_files=inputs,
        output_files=outputs,
        allow_empty_output_roles=("stderr",),
        minimum_free_gib=args.minimum_free_gib,
    )
    _print(record)
    return 0


def _running(args: argparse.Namespace) -> int:
    _print(mark_external_scratch_running(Path(args.record)))
    return 0


def _copy_back(args: argparse.Namespace) -> int:
    _print(
        copy_back_external_scratch(
            Path(args.record), process_exit_code=args.process_exit_code
        )
    )
    return 0


def _fail(args: argparse.Namespace) -> int:
    _print(mark_external_scratch_failed(Path(args.record), reason=args.reason))
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    _print(
        cleanup_external_scratch(
            Path(args.record),
            finalization_metadata_path=Path(args.finalization_metadata),
        )
    )
    return 0


def _inspect(args: argparse.Namespace) -> int:
    _print(read_json(Path(args.record)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--scratch-root", required=True)
    prepare.add_argument("--mount", required=True)
    prepare.add_argument("--minimum-free-gib", type=float, required=True)
    prepare.add_argument("--layout", required=True)
    prepare.add_argument("--target", required=True)
    prepare.add_argument("--input", required=True)
    prepare.add_argument("--geometry-contract", default="")
    prepare.add_argument("--execution-input", action="append", default=[])
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--stderr", required=True)
    prepare.add_argument("--artifact", action="append", default=[])
    prepare.set_defaults(handler=_prepare)

    running = sub.add_parser("running")
    running.add_argument("--record", required=True)
    running.set_defaults(handler=_running)

    copy_back = sub.add_parser("copy-back")
    copy_back.add_argument("--record", required=True)
    copy_back.add_argument("--process-exit-code", type=int, required=True)
    copy_back.set_defaults(handler=_copy_back)

    fail = sub.add_parser("fail")
    fail.add_argument("--record", required=True)
    fail.add_argument("--reason", required=True)
    fail.set_defaults(handler=_fail)

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--record", required=True)
    cleanup.add_argument("--finalization-metadata", required=True)
    cleanup.set_defaults(handler=_cleanup)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--record", required=True)
    inspect.set_defaults(handler=_inspect)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (ExternalScratchError, OSError, ValueError, KeyError, TypeError) as exc:
        _print(
            {
                "status": "FAILED",
                "code": getattr(exc, "code", "FAILED_EXTERNAL_SCRATCH"),
                "reason": str(exc),
            }
        )
        return 74


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "EXTERNAL_SCRATCH_RECORD"]
