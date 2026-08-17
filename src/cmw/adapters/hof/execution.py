"""Materialize dependency-resolved HOF ORCA plans without launching ORCA."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shlex
import shutil
import tempfile
from typing import Any

from cmw.core.artifacts import OptimizationArtifact, artifact_from_dict
from cmw.core.execution_layout import (
    ExecutionLayout,
    execution_target_directory,
    next_attempt_identifier,
)
from cmw.core.job import GeometryLineage
from cmw.core.provenance import atomic_write_json, read_json
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.molecular.orca.input import make_target_from_geometry_input, render_orca_input
from cmw.molecular.orca.job import check_reuse, write_target
from cmw.molecular.orca.status import StageType

from .config import load_hof_configuration
from .planner import build_hof_workflow_plan
from .orca import prepare_hof_orca_geometry_input
from .workflow import _node_token


def _artifact_path(metadata_path: Path, value: dict[str, Any]) -> Path:
    path = Path(str(value.get("path", "")))
    return path if path.is_absolute() else metadata_path.parent / path


def _validated_relaxation_result(
    metadata_path: Path, *, fragment_id: str
) -> tuple[dict[str, Any], Path, OptimizationArtifact]:
    record = read_json(metadata_path)
    layout = record.get("execution_layout")
    if not isinstance(layout, dict):
        raise ValueError("fragment relaxation result lacks an execution layout")
    target_path = Path(str(layout["target_directory"])) / "target.json"
    decision = check_reuse(target_path, metadata_path)
    if not decision["reuse"]:
        raise ValueError(
            "fragment relaxation result is not reusable: " + str(decision["reason"])
        )
    protocol = record.get("target", {}).get("calculation", {}).get("protocol", {})
    if not isinstance(protocol, dict) or protocol.get("fragment_id") != fragment_id:
        raise ValueError("fragment relaxation result has the wrong fragment identity")
    final_geometry = record.get("artifacts", {}).get("final_geometry")
    if not isinstance(final_geometry, dict):
        raise ValueError("fragment relaxation result lacks final_geometry")
    geometry_path = _artifact_path(metadata_path, final_geometry).resolve(strict=True)
    typed = record.get("scientific_artifact")
    if not isinstance(typed, dict):
        raise ValueError("fragment relaxation result lacks a typed artifact")
    artifact = artifact_from_dict(typed)
    if not isinstance(artifact, OptimizationArtifact) or not artifact.validation.passed:
        raise ValueError("fragment relaxation typed artifact is not a validated optimization")
    return record, geometry_path, artifact


def _find_reusable_result(target_directory: Path) -> Path | None:
    attempts = target_directory / "attempts"
    if not attempts.is_dir():
        return None
    target_path = target_directory / "target.json"
    for attempt in sorted(attempts.iterdir(), reverse=True):
        metadata = attempt / "job.json"
        if metadata.is_file() and check_reuse(target_path, metadata)["reuse"]:
            return metadata
    return None


def _command_text(
    *,
    cmw_root: Path,
    python_bin: Path,
    orca_executable: Path,
    layout: ExecutionLayout,
    runtime_contract: Path,
    nprocs: int,
    maxcore: int,
) -> str:
    command = [
        str(cmw_root / "scripts/orca/run_orca.sh"),
        "--input",
        str(layout.input_path("stage.inp")),
        "--target",
        str(layout.target_directory / "target.json"),
        "--metadata",
        str(layout.metadata_path("job.json")),
        "--output",
        str(layout.output_path("stage.out")),
        "--stderr",
        str(layout.log_path("stage.err")),
        "--layout",
        str(layout.layout_path),
        "--geometry-contract",
        str(layout.metadata_path("geometry-input.json")),
        "--runtime-contract",
        str(runtime_contract),
        "--require-runtime-contract",
    ]
    environment = [
        "PYTHONDONTWRITEBYTECODE=1",
        f"PYTHON_BIN={shlex.quote(str(python_bin))}",
        f"ORCA_EXE={shlex.quote(str(orca_executable))}",
        f"NPROCS={nprocs}",
        f"MAXCORE_MB={maxcore}",
        "CMW_REQUIRE_MINIMUM=0",
        "CMW_IMAGINARY_TOLERANCE_CM1=0.0",
    ]
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + " ".join(
        (*environment, shlex.join(command))
    ) + "\n"


def materialize_relaxed_fragment_energy(
    *,
    systems_path: Path,
    methods_path: Path,
    protocol_path: Path,
    execution_path: Path,
    system_id: str,
    project_root: Path,
    fragment_id: str,
    relaxation_result: Path,
    runtime_contract_source: Path,
    cmw_root: Path,
    python_bin: Path,
    orca_executable: Path,
) -> dict[str, object]:
    """Resolve a relaxed geometry and materialize its high-level energy attempt."""

    configuration = load_hof_configuration(
        systems_path=systems_path,
        methods_path=methods_path,
        protocol_path=protocol_path,
        execution_path=execution_path,
        system_id=system_id,
        project_root=project_root,
    )
    plan = build_hof_workflow_plan(configuration)
    fragment = configuration.system.fragment_map.get(fragment_id)
    if fragment is None:
        raise ValueError(f"unknown HOF fragment: {fragment_id}")
    node_id = f"relaxed_fragment_{_node_token(fragment_id)}_energy"
    template = plan.orca_calculations.get(node_id)
    if template is None or template.spec.stage_type is not StageType.SP:
        raise ValueError(f"HOF plan lacks a relaxed-fragment SP node: {node_id}")
    relaxation, geometry_path, optimization = _validated_relaxation_result(
        relaxation_result, fragment_id=fragment_id
    )
    source_target_id = str(relaxation["target"]["target_id"])
    structure = structure_artifact_from_file(
        geometry_path,
        source="validated_isolated_fragment_optimization",
        producing_calculation=source_target_id,
        charge=fragment.charge,
        multiplicity=fragment.multiplicity,
        provenance={
            "adapter": "cmw.adapters.hof",
            "source_result": str(relaxation_result.resolve()),
            "source_optimization_artifact": optimization.artifact_id,
        },
        metadata={
            "fragment_id": fragment_id,
            "structure_role": "relaxed",
        },
    )
    calculation = replace(template, geometry_artifact=structure)
    if calculation.resources is None:
        raise ValueError("relaxed-fragment execution requires resolved ORCA resources")
    with tempfile.TemporaryDirectory() as temporary:
        temporary_geometry = Path(temporary) / "input.xyz"
        temporary_input = prepare_hof_orca_geometry_input(
            system=configuration.system,
            calculation=calculation,
            geometry_path=temporary_geometry,
        )
        target = make_target_from_geometry_input(
            temporary_input, spec=calculation.spec
        )
    target_directory = execution_target_directory(
        project_root,
        system_identifier=system_id,
        workflow_node_identifier=node_id,
        target_identifier=target.target_id,
    )
    reusable = _find_reusable_result(target_directory)
    if reusable is not None:
        return {
            "status": "REUSED",
            "node_id": node_id,
            "target_id": target.target_id,
            "result_path": str(reusable),
        }
    attempt_id = next_attempt_identifier(target_directory)
    layout = ExecutionLayout(
        project_root=project_root,
        system_identifier=system_id,
        workflow_node_identifier=node_id,
        target_identifier=target.target_id,
        attempt_identifier=attempt_id,
    )
    layout.create_working_directory()
    geometry_input = prepare_hof_orca_geometry_input(
        system=configuration.system,
        calculation=calculation,
        geometry_path=layout.input_path("input.xyz"),
    )
    materialized_target = make_target_from_geometry_input(
        geometry_input, spec=calculation.spec
    )
    if materialized_target.target_id != target.target_id:
        raise ValueError("materialized relaxed geometry changed target identity")
    target_path = target_directory / "target.json"
    lineage = GeometryLineage(
        source="validated_isolated_fragment_optimization",
        geometry_sha256=target.geometry_sha256,
        parent_stage="OPT",
        parent_target_id=source_target_id,
        parent_artifact_sha256=target.geometry_sha256,
    )
    if target_path.exists():
        stored = read_json(target_path)
        if stored.get("target", {}).get("target_id") != target.target_id:
            raise FileExistsError("stored relaxed-fragment target conflicts")
    else:
        write_target(target_path, target, lineage)
    atomic_write_json(layout.layout_path, layout.to_dict())
    geometry_contract = layout.metadata_path("geometry-input.json")
    atomic_write_json(geometry_contract, geometry_input.to_dict())
    layout.input_path("stage.inp").write_text(
        render_orca_input(
            geometry_input=geometry_input,
            charge=calculation.charge,
            multiplicity=calculation.multiplicity,
            spec=calculation.spec,
            resources=calculation.resources,
        ),
        encoding="utf-8",
    )
    runtime_source = runtime_contract_source.expanduser().resolve(strict=True)
    runtime_contract = layout.metadata_path("orca-runtime.json")
    shutil.copy2(runtime_source, runtime_contract)
    command_path = layout.metadata_path("exact_terminal_command.sh")
    command_path.write_text(
        _command_text(
            cmw_root=cmw_root.resolve(),
            python_bin=python_bin.resolve(),
            orca_executable=orca_executable.resolve(),
            layout=layout,
            runtime_contract=runtime_contract,
            nprocs=calculation.resources.nprocs,
            maxcore=calculation.resources.maxcore_mb_per_process,
        ),
        encoding="utf-8",
    )
    command_path.chmod(0o750)
    return {
        "status": "PREPARED",
        "node_id": node_id,
        "target_id": target.target_id,
        "attempt_id": attempt_id,
        "attempt_directory": str(layout.working_directory),
        "result_path": str(layout.metadata_path("job.json")),
        "command_path": str(command_path),
        "source_structure_artifact": structure.to_dict(),
    }


__all__ = ["materialize_relaxed_fragment_energy"]
