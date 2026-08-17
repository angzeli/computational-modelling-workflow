"""Machine-readable shell bridge for ORCA runtime semantics."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cmw.core.execution_layout import ExecutionLayout
from cmw.core.execution_profiles import load_execution_profiles
from cmw.core.job import GeometryLineage
from cmw.core.locks import acquire_lock, inspect_lock, release_lock
from cmw.core.provenance import atomic_write_json
from cmw.core.structure_artifacts import structure_artifact_from_file

from .geometry import OrcaGeometryInput, prepare_orca_geometry_input
from .input import (
    OrcaResources,
    OrcaStageSpec,
    make_target_from_geometry_input,
    render_orca_input,
    resolve_orca_resources,
)
from .job import check_reuse, finalize_attempt, write_target
from .runtime import (
    materialize_orca_runtime_contract,
    prepare_orca_runtime,
    runtime_environment,
    validate_orca_runtime_contract,
)
from .status import FrequencyPolicy, StageType, classify_execution, read_orca_output, validate_stage


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _prepare(args: argparse.Namespace) -> int:
    spec = OrcaStageSpec(StageType(args.stage), args.keywords, tuple(args.block))
    execution: dict[str, object] | None = None
    if args.execution_config is not None:
        resolved = resolve_orca_resources(
            load_execution_profiles(Path(args.execution_config)).selected
        )
        if args.nprocs is not None and args.nprocs != resolved.resources.nprocs:
            raise ValueError("explicit nprocs contradicts the execution profile")
        if (
            args.maxcore is not None
            and args.maxcore != resolved.resources.maxcore_mb_per_process
        ):
            raise ValueError("explicit maxcore contradicts the execution profile")
        resources = resolved.resources
        execution = resolved.to_dict()
    else:
        resources = OrcaResources(
            args.nprocs if args.nprocs is not None else 1,
            args.maxcore if args.maxcore is not None else 1000,
        )
    input_path = Path(args.input)
    source_artifact = structure_artifact_from_file(
        Path(args.structure).resolve(),
        source=args.lineage_source,
        charge=args.charge,
        multiplicity=args.multiplicity,
        provenance={"adapter": "cmw.molecular.orca.cli"},
    )
    geometry_path = (input_path.parent / "input.xyz").resolve()
    geometry_input = prepare_orca_geometry_input(
        source_artifact,
        geometry_path,
        provenance={"preparation": "orca_cli"},
    )
    target = make_target_from_geometry_input(geometry_input, spec=spec)
    lineage = GeometryLineage(
        source=args.lineage_source,
        geometry_sha256=target.geometry_sha256,
        parent_stage=args.parent_stage or None,
        parent_target_id=args.parent_target_id or None,
        parent_artifact_sha256=args.parent_artifact_sha256 or None,
    )
    geometry_contract_path = input_path.parent / "geometry-input.json"
    atomic_write_json(geometry_contract_path, geometry_input.to_dict())
    input_path.write_text(
        render_orca_input(
            geometry_input=geometry_input,
            charge=args.charge,
            multiplicity=args.multiplicity,
            spec=spec,
            resources=resources,
        ),
        encoding="utf-8",
    )
    write_target(Path(args.target), target, lineage)
    result: dict[str, object] = {
        "target_id": target.target_id,
        "input": str(input_path),
        "target": args.target,
        "resources": resources.to_dict(),
        "execution_intent": spec.execution_intent.to_dict(),
        "geometry_contract": str(geometry_contract_path),
        "geometry_input": geometry_input.to_dict(),
    }
    if execution is not None:
        result["execution"] = execution
    _print(result)
    return 0


def _status(args: argparse.Namespace) -> int:
    evidence = read_orca_output(Path(args.output), stderr_path=Path(args.stderr) if args.stderr else None)
    execution = classify_execution(
        evidence, process_running=args.running, process_exit_code=args.process_exit_code
    )
    scientific = validate_stage(
        evidence,
        StageType(args.stage),
        execution=execution,
        frequency_policy=FrequencyPolicy(args.require_minimum, args.imaginary_tolerance),
    )
    _print(
        {
            "schema_version": 1,
            "execution": {**execution.__dict__, "status": execution.status.value},
            "scientific": {**scientific.__dict__, "status": scientific.status.value, "stage_type": scientific.stage_type.value},
            "evidence": evidence.to_dict(),
        }
    )
    return 0 if scientific.valid else 70


def _finalize(args: argparse.Namespace) -> int:
    artifacts: dict[str, Path] = {}
    for value in args.artifact:
        role, separator, path = value.partition("=")
        if not separator or not role or not path:
            raise ValueError(f"invalid artifact declaration: {value!r}")
        artifacts[role] = Path(path)
    record = finalize_attempt(
        target_path=Path(args.target),
        metadata_path=Path(args.metadata),
        input_path=Path(args.input),
        output_path=Path(args.output),
        stderr_path=Path(args.stderr),
        process_exit_code=args.process_exit_code,
        executable={"path": args.orca_exe, "version": args.orca_version or None},
        resources={"nprocs": args.nprocs, "maxcore_mb_per_process": args.maxcore},
        artifacts=artifacts,
        repository=Path(args.repository) if args.repository else None,
        frequency_policy=FrequencyPolicy(args.require_minimum, args.imaginary_tolerance),
        parent_attempt_id=args.parent_attempt_id or None,
        execution_layout=(
            ExecutionLayout.from_mapping(
                json.loads(Path(args.layout).read_text(encoding="utf-8"))
            )
            if args.layout
            else None
        ),
        geometry_input=(
            OrcaGeometryInput.from_mapping(
                json.loads(Path(args.geometry_contract).read_text(encoding="utf-8"))
            )
            if args.geometry_contract
            else None
        ),
        geometry_contract_path=(
            Path(args.geometry_contract) if args.geometry_contract else None
        ),
    )
    _print(record)
    return 0 if record["reusable"] else 70


def _reuse(args: argparse.Namespace) -> int:
    result = check_reuse(Path(args.target), Path(args.metadata))
    _print(result)
    return 0 if result["reuse"] else 3


def _runtime_prepare(args: argparse.Namespace) -> int:
    profile = load_execution_profiles(Path(args.execution_config)).selected
    record = prepare_orca_runtime(
        profile, orca_executable=Path(args.orca_exe)
    )
    atomic_write_json(Path(args.output), record)
    _print(record)
    return 0


def _runtime_validate(args: argparse.Namespace) -> int:
    value = json.loads(Path(args.runtime).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ORCA runtime contract must be a mapping")
    record = validate_orca_runtime_contract(
        value, orca_executable=Path(args.orca_exe)
    )
    _print(
        {
            "runtime_id": record["runtime_id"],
            "environment": runtime_environment(record),
            "launch_overlay": record["launch_overlay"],
            "validation": record["validation"],
        }
    )
    return 0


def _runtime_materialize(args: argparse.Namespace) -> int:
    value = json.loads(Path(args.runtime).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ORCA runtime contract must be a mapping")
    record = materialize_orca_runtime_contract(
        value,
        orca_executable=Path(args.orca_exe),
        working_directory=Path(args.working_directory),
    )
    atomic_write_json(Path(args.output), record)
    _print(record)
    return 0


def _lock(args: argparse.Namespace) -> int:
    path = Path(args.lock)
    if args.lock_action == "inspect":
        result = inspect_lock(path)
        _print({"state": result.state.value, "reason": result.reason, "owner": result.owner.__dict__ if result.owner else None})
        return 0
    if args.lock_action == "acquire":
        try:
            owner = acquire_lock(
                path, job_id=args.job_id, owner_pid=args.owner_pid, replace_stale=args.replace_stale
            )
        except RuntimeError as exc:
            _print({"acquired": False, "reason": str(exc)})
            return 5
        _print({"acquired": True, "owner": owner.__dict__})
        return 0
    released = release_lock(path, token=args.token)
    _print({"released": released})
    return 0 if released else 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--stage", choices=[item.value for item in StageType], required=True)
    prepare.add_argument("--structure", required=True)
    prepare.add_argument("--charge", type=int, required=True)
    prepare.add_argument("--multiplicity", type=int, required=True)
    prepare.add_argument("--keywords", required=True)
    prepare.add_argument("--block", action="append", default=[])
    prepare.add_argument("--nprocs", type=int)
    prepare.add_argument("--maxcore", type=int)
    prepare.add_argument("--execution-config")
    prepare.add_argument("--input", required=True)
    prepare.add_argument("--target", required=True)
    prepare.add_argument("--lineage-source", default="input_structure")
    prepare.add_argument("--parent-stage", default="")
    prepare.add_argument("--parent-target-id", default="")
    prepare.add_argument("--parent-artifact-sha256", default="")
    prepare.set_defaults(handler=_prepare)

    status = sub.add_parser("status")
    status.add_argument("--stage", choices=[item.value for item in StageType], required=True)
    status.add_argument("--output", required=True)
    status.add_argument("--stderr", default="")
    status.add_argument("--process-exit-code", type=int)
    status.add_argument("--running", action="store_true")
    status.add_argument("--require-minimum", action="store_true")
    status.add_argument("--imaginary-tolerance", type=float, default=0.0)
    status.set_defaults(handler=_status)

    finalize = sub.add_parser("finalize")
    finalize.add_argument("--target", required=True)
    finalize.add_argument("--metadata", required=True)
    finalize.add_argument("--input", required=True)
    finalize.add_argument("--output", required=True)
    finalize.add_argument("--stderr", required=True)
    finalize.add_argument("--process-exit-code", type=int, required=True)
    finalize.add_argument("--orca-exe", required=True)
    finalize.add_argument("--orca-version", default="")
    finalize.add_argument("--nprocs", type=int, default=1)
    finalize.add_argument("--maxcore", type=int, default=1000)
    finalize.add_argument("--artifact", action="append", default=[])
    finalize.add_argument("--repository", default="")
    finalize.add_argument("--require-minimum", action="store_true")
    finalize.add_argument("--imaginary-tolerance", type=float, default=0.0)
    finalize.add_argument("--parent-attempt-id", default="")
    finalize.add_argument("--layout", default="")
    finalize.add_argument("--geometry-contract", default="")
    finalize.set_defaults(handler=_finalize)

    reuse = sub.add_parser("reuse")
    reuse.add_argument("--target", required=True)
    reuse.add_argument("--metadata", required=True)
    reuse.set_defaults(handler=_reuse)

    runtime_prepare = sub.add_parser("runtime-prepare")
    runtime_prepare.add_argument("--execution-config", required=True)
    runtime_prepare.add_argument("--orca-exe", required=True)
    runtime_prepare.add_argument("--output", required=True)
    runtime_prepare.set_defaults(handler=_runtime_prepare)

    runtime_validate = sub.add_parser("runtime-validate")
    runtime_validate.add_argument("--runtime", required=True)
    runtime_validate.add_argument("--orca-exe", required=True)
    runtime_validate.set_defaults(handler=_runtime_validate)

    runtime_materialize = sub.add_parser("runtime-materialize")
    runtime_materialize.add_argument("--runtime", required=True)
    runtime_materialize.add_argument("--orca-exe", required=True)
    runtime_materialize.add_argument("--working-directory", required=True)
    runtime_materialize.add_argument("--output", required=True)
    runtime_materialize.set_defaults(handler=_runtime_materialize)

    lock = sub.add_parser("lock")
    lock_sub = lock.add_subparsers(dest="lock_action", required=True)
    inspect = lock_sub.add_parser("inspect")
    inspect.add_argument("--lock", required=True)
    inspect.set_defaults(handler=_lock)
    acquire = lock_sub.add_parser("acquire")
    acquire.add_argument("--lock", required=True)
    acquire.add_argument("--job-id", default="")
    acquire.add_argument("--owner-pid", type=int, default=os.getpid())
    acquire.add_argument("--replace-stale", action="store_true")
    acquire.set_defaults(handler=_lock)
    release = lock_sub.add_parser("release")
    release.add_argument("--lock", required=True)
    release.add_argument("--token", required=True)
    release.set_defaults(handler=_lock)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return int(arguments.handler(arguments))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        _print({"error": type(exc).__name__, "reason": str(exc)})
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
