"""Materialize dependency-resolved HOF ORCA plans without launching ORCA."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shlex
import shutil
import tempfile
from typing import Any

from cmw.core.artifacts import (
    ArtifactValidation,
    FrequencyArtifact,
    OptimizationArtifact,
    StructureArtifact,
    ValidationStatus,
    artifact_from_dict,
)
from cmw.core.execution_layout import (
    ExecutionLayout,
    ExecutionLayoutVersion,
    TARGET_MANIFEST_FILENAME,
    build_target_manifest,
    execution_target_directory_v2,
    next_attempt_identifier,
    resolve_recorded_layout,
    write_target_manifest,
)
from cmw.core.job import GeometryLineage
from cmw.core.provenance import atomic_write_json, canonical_json_bytes, read_json
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
    target_path = (
        resolve_recorded_layout(layout, metadata_path=metadata_path).target_directory
        / "target.json"
    )
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


def _validated_result(
    metadata_path: Path, *, expected_stage: StageType
) -> tuple[dict[str, Any], object]:
    record = read_json(metadata_path)
    layout = record.get("execution_layout")
    if not isinstance(layout, dict):
        raise ValueError("parent result lacks an execution layout")
    target_path = Path(str(layout["target_directory"])) / "target.json"
    decision = check_reuse(target_path, metadata_path)
    if not decision["reuse"]:
        raise ValueError("parent result is not reusable: " + str(decision["reason"]))
    if record.get("target", {}).get("stage_type") != expected_stage.value:
        raise ValueError(f"parent result is not a validated {expected_stage.value} stage")
    typed = record.get("scientific_artifact")
    if not isinstance(typed, dict):
        raise ValueError("parent result lacks a typed scientific artifact")
    artifact = artifact_from_dict(typed)
    return record, artifact


def _validated_optimized_structure(
    metadata_path: Path, *, system_id: str
) -> tuple[dict[str, Any], StructureArtifact]:
    record, artifact = _validated_result(metadata_path, expected_stage=StageType.OPT)
    if not isinstance(artifact, OptimizationArtifact) or not artifact.validation.passed:
        raise ValueError("parent result is not a validated OptimizationArtifact")
    final_geometry = record.get("artifacts", {}).get("final_geometry")
    if not isinstance(final_geometry, dict):
        raise ValueError("optimization result lacks final_geometry")
    geometry_path = _artifact_path(metadata_path, final_geometry).resolve(strict=True)
    structure = structure_artifact_from_file(
        geometry_path,
        source="validated_geometry_optimization",
        producing_calculation=str(record["target"]["target_id"]),
        charge=int(record["target"]["charge"]),
        multiplicity=int(record["target"]["multiplicity"]),
        provenance={
            "adapter": "cmw.adapters.hof",
            "source_result": str(metadata_path.resolve()),
            "source_optimization_artifact": artifact.artifact_id,
        },
        metadata={"system_id": system_id, "structure_role": "optimized"},
    )
    return record, structure


def _validate_frequency_parent(
    metadata_path: Path, *, expected_geometry_hash: str
) -> dict[str, Any]:
    record, artifact = _validated_result(metadata_path, expected_stage=StageType.FREQ)
    if not isinstance(artifact, FrequencyArtifact) or not artifact.validation.passed:
        raise ValueError("frequency result is not a validated FrequencyArtifact")
    if record["target"]["geometry_sha256"] != expected_geometry_hash:
        raise ValueError("frequency result does not validate the optimized geometry")
    return record


def promote_hof_geometry(
    *,
    optimization_result: Path,
    frequency_result: Path,
    system_id: str,
    output_path: Path,
) -> dict[str, object]:
    """Promote one geometry only after reusable OPT and FREQ results agree."""

    optimization, structure = _validated_optimized_structure(
        optimization_result, system_id=system_id
    )
    frequency, frequency_artifact = _validated_result(
        frequency_result, expected_stage=StageType.FREQ
    )
    if not isinstance(frequency_artifact, FrequencyArtifact):
        raise ValueError("frequency result lacks a FrequencyArtifact")
    if frequency["target"]["geometry_sha256"] != structure.geometry_hash:
        raise ValueError("frequency result does not validate the optimized geometry")
    promoted = StructureArtifact(
        producing_calculation=str(frequency["target"]["target_id"]),
        method=structure.method,
        basis=structure.basis,
        protocol={
            "validation_contract": "optimization_plus_frequency",
            "optimization_target": optimization["target"]["target_id"],
            "frequency_target": frequency["target"]["target_id"],
        },
        parent_artifacts=(structure.artifact_id, frequency_artifact.artifact_id),
        files=dict(structure.files),
        validation=ArtifactValidation(
            ValidationStatus.PASSED,
            {"optimization_reusable": True, "frequency_reusable": True},
            "VALIDATED_OPT_FREQ_GEOMETRY",
        ),
        provenance={
            **dict(structure.provenance),
            "optimization_result": str(optimization_result.resolve()),
            "frequency_result": str(frequency_result.resolve()),
        },
        metadata={
            **dict(structure.metadata),
            "structure_role": "validated_optimized",
        },
        source="validated_opt_freq",
        format=structure.format,
        atom_count=structure.atom_count,
        elemental_composition=structure.elemental_composition,
        charge=structure.charge,
        multiplicity=structure.multiplicity,
        geometry_hash=structure.geometry_hash,
    )
    record = {
        "schema_version": 1,
        "system_id": system_id,
        "status": "PASSED",
        "artifact": promoted.to_dict(),
    }
    atomic_write_json(output_path, record)
    return record


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


def _reused_attempt_state(
    metadata: Path, *, node_id: str, target_id: str
) -> dict[str, object]:
    """Return the same operational identity fields as a prepared attempt."""

    attempt = metadata.resolve().parent
    command = attempt / "exact_terminal_command.sh"
    if not command.is_file():
        raise ValueError("reusable HOF attempt lacks its launch provenance command")
    result: dict[str, object] = {
        "status": "REUSED",
        "node_id": node_id,
        "target_id": target_id,
        "attempt_id": attempt.name,
        "target_directory": str(attempt.parent.parent),
        "attempt_directory": str(attempt),
        "result_path": str(metadata.resolve()),
        "command_path": str(command),
    }
    layout_path = attempt / "execution-layout.json"
    if layout_path.is_file():
        layout = read_json(layout_path)
        expected = {
            "attempt_identifier": attempt.name,
            "target_identifier": target_id,
            "workflow_node_identifier": node_id,
        }
        if any(layout.get(name) != value for name, value in expected.items()):
            raise ValueError("reusable HOF attempt has conflicting execution identity")
    return result


def _find_pristine_prepared_attempt(
    target_directory: Path, *, runtime_contract_source: Path
) -> dict[str, object] | None:
    attempts = target_directory / "attempts"
    if not attempts.is_dir():
        return None
    expected_runtime = read_json(
        runtime_contract_source.expanduser().resolve(strict=True)
    )
    for attempt in sorted(attempts.iterdir(), reverse=True):
        required = {
            "stage.inp",
            "input.xyz",
            "geometry-input.json",
            "execution-layout.json",
            "orca-runtime.json",
            "exact_terminal_command.sh",
        }
        if not attempt.is_dir() or not all((attempt / name).is_file() for name in required):
            continue
        spent = {"job.json", "stage.out", "stage.err"}
        if any((attempt / name).exists() for name in spent):
            continue
        stored_runtime = read_json(attempt / "orca-runtime.json")
        if canonical_json_bytes(stored_runtime) != canonical_json_bytes(
            expected_runtime
        ):
            continue
        layout = read_json(attempt / "execution-layout.json")
        resolved_layout = resolve_recorded_layout(
            layout, metadata_path=attempt / "execution-layout.json"
        )
        if resolved_layout.target_directory != target_directory:
            raise ValueError("prepared attempt has a conflicting target directory")
        return {
            "status": "PREPARED",
            "node_id": resolved_layout.workflow_node_identifier,
            "target_id": resolved_layout.target_identifier,
            "attempt_id": resolved_layout.attempt_identifier,
            "target_directory": str(target_directory),
            "attempt_directory": str(attempt),
            "result_path": str(attempt / "job.json"),
            "command_path": str(attempt / "exact_terminal_command.sh"),
        }
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
    minimum_free_disk_gb: float | None = None,
    disk_check_path: Path | None = None,
    artifacts: dict[str, Path] | None = None,
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
    for role, path in (artifacts or {}).items():
        command.extend(("--artifact", f"{role}={path}"))
    if minimum_free_disk_gb is not None:
        if disk_check_path is None:
            raise ValueError("disk-capacity policy requires a check path")
        command.extend(
            (
                "--minimum-free-disk-gb",
                str(minimum_free_disk_gb),
                "--disk-check-path",
                str(disk_check_path),
            )
        )
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


def _materialize_calculation(
    *,
    configuration: Any,
    calculation: Any,
    project_root: Path,
    source: str,
    parent_stage: StageType | None,
    parent_target_id: str | None,
    runtime_contract_source: Path,
    cmw_root: Path,
    python_bin: Path,
    orca_executable: Path,
) -> dict[str, object]:
    if calculation.resources is None:
        raise ValueError("HOF execution requires resolved ORCA resources")
    with tempfile.TemporaryDirectory() as temporary:
        temporary_input = prepare_hof_orca_geometry_input(
            system=configuration.system,
            calculation=calculation,
            geometry_path=Path(temporary) / "input.xyz",
        )
        target = make_target_from_geometry_input(temporary_input, spec=calculation.spec)
    target_directory = execution_target_directory_v2(
        project_root,
        target_identifier=target.target_id,
    )
    reusable = _find_reusable_result(target_directory)
    if reusable is not None:
        return _reused_attempt_state(
            reusable, node_id=calculation.node_id, target_id=target.target_id
        )
    prepared = _find_pristine_prepared_attempt(
        target_directory, runtime_contract_source=runtime_contract_source
    )
    if prepared is not None:
        return prepared
    layout = ExecutionLayout(
        project_root=project_root,
        system_identifier=configuration.system.system_id,
        workflow_node_identifier=calculation.node_id,
        target_identifier=target.target_id,
        attempt_identifier=next_attempt_identifier(target_directory),
        version=ExecutionLayoutVersion.V2,
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
        raise ValueError("materialized geometry changed target identity")
    target_path = target_directory / "target.json"
    lineage = GeometryLineage(
        source=source,
        geometry_sha256=target.geometry_sha256,
        parent_stage=parent_stage.value if parent_stage is not None else None,
        parent_target_id=parent_target_id,
        parent_artifact_sha256=(
            target.geometry_sha256 if parent_target_id is not None else None
        ),
    )
    if target_path.exists():
        stored = read_json(target_path)
        if stored.get("target", {}).get("target_id") != target.target_id:
            raise FileExistsError("stored HOF target conflicts")
    else:
        write_target(target_path, target, lineage)
    write_target_manifest(
        target_directory / TARGET_MANIFEST_FILENAME,
        build_target_manifest(
            layout,
            target=target.to_dict(),
            execution_intent=calculation.spec.execution_intent.to_dict(),
            provenance={"source": source},
        ),
    )
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
    artifact_paths: dict[str, Path] = {}
    if calculation.spec.stage_type is StageType.OPT:
        artifact_paths["final_geometry"] = layout.output_path("stage.xyz")
    if calculation.node_id == "igmh_density":
        artifact_paths["wavefunction"] = layout.output_path("stage.gbw")
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
            minimum_free_disk_gb=(
                configuration.execution_profile.storage.minimum_free_gb
                if configuration.execution_profile is not None
                and configuration.execution_profile.storage is not None
                else None
            ),
            disk_check_path=project_root,
            artifacts=artifact_paths,
        ),
        encoding="utf-8",
    )
    command_path.chmod(0o750)
    return {
        "status": "PREPARED",
        "node_id": calculation.node_id,
        "target_id": target.target_id,
        "attempt_id": layout.attempt_identifier,
        "target_directory": str(target_directory),
        "attempt_directory": str(layout.working_directory),
        "result_path": str(layout.metadata_path("job.json")),
        "command_path": str(command_path),
        "geometry_hash": target.geometry_sha256,
    }


def materialize_hof_orca_node(
    *,
    systems_path: Path,
    methods_path: Path,
    protocol_path: Path,
    execution_path: Path,
    system_id: str,
    project_root: Path,
    node_id: str,
    runtime_contract_source: Path,
    cmw_root: Path,
    python_bin: Path,
    orca_executable: Path,
    optimization_result: Path | None = None,
    frequency_result: Path | None = None,
    geometry_result: Path | None = None,
) -> dict[str, object]:
    """Materialize one planned HOF ORCA node after validating its geometry gates."""

    configuration = load_hof_configuration(
        systems_path=systems_path,
        methods_path=methods_path,
        protocol_path=protocol_path,
        execution_path=execution_path,
        system_id=system_id,
        project_root=project_root,
    )
    plan = build_hof_workflow_plan(configuration)
    template = plan.orca_calculations.get(node_id)
    if template is None:
        raise ValueError(f"HOF plan has no ORCA node: {node_id}")

    calculation = template
    source = "input_structure"
    parent_stage: StageType | None = None
    parent_target_id: str | None = None
    if node_id == "geometry_optimization":
        if any(value is not None for value in (optimization_result, frequency_result, geometry_result)):
            raise ValueError("initial geometry optimization does not accept parent results")
    else:
        selected_result = geometry_result or optimization_result
        if selected_result is None:
            raise ValueError(f"{node_id} requires a validated geometry result")
        parent, structure = _validated_optimized_structure(
            selected_result, system_id=system_id
        )
        if node_id != "geometry_frequency":
            if frequency_result is None:
                raise ValueError(f"{node_id} requires validated frequency evidence")
            _validate_frequency_parent(
                frequency_result, expected_geometry_hash=structure.geometry_hash
            )
            source = "validated_opt_freq"
        else:
            source = "validated_geometry_optimization"
        calculation = replace(template, geometry_artifact=structure)
        parent_stage = StageType.OPT
        parent_target_id = str(parent["target"]["target_id"])

    return _materialize_calculation(
        configuration=configuration,
        calculation=calculation,
        project_root=project_root,
        source=source,
        parent_stage=parent_stage,
        parent_target_id=parent_target_id,
        runtime_contract_source=runtime_contract_source,
        cmw_root=cmw_root,
        python_bin=python_bin,
        orca_executable=orca_executable,
    )


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
    target_directory = execution_target_directory_v2(
        project_root,
        target_identifier=target.target_id,
    )
    reusable = _find_reusable_result(target_directory)
    if reusable is not None:
        return _reused_attempt_state(
            reusable, node_id=node_id, target_id=target.target_id
        )
    attempt_id = next_attempt_identifier(target_directory)
    layout = ExecutionLayout(
        project_root=project_root,
        system_identifier=system_id,
        workflow_node_identifier=node_id,
        target_identifier=target.target_id,
        attempt_identifier=attempt_id,
        version=ExecutionLayoutVersion.V2,
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
    write_target_manifest(
        target_directory / TARGET_MANIFEST_FILENAME,
        build_target_manifest(
            layout,
            target=target.to_dict(),
            execution_intent=calculation.spec.execution_intent.to_dict(),
            provenance={"source": "validated_isolated_fragment_optimization"},
        ),
    )
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
            minimum_free_disk_gb=(
                configuration.execution_profile.storage.minimum_free_gb
                if configuration.execution_profile is not None
                and configuration.execution_profile.storage is not None
                else None
            ),
            disk_check_path=project_root,
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


__all__ = [
    "materialize_hof_orca_node",
    "materialize_relaxed_fragment_energy",
    "promote_hof_geometry",
]
