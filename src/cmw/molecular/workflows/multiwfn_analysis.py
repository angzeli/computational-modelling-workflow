"""Shared target, attempt, artifact, and reuse semantics for Multiwfn analyses."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from cmw.core.artifacts import (
    Artifact,
    DensityArtifact,
    IGMHArtifact,
    artifact_from_dict,
    artifact_from_result,
)
from cmw.core.job import ExecutionAttempt, JobTarget
from cmw.core.provenance import (
    ArtifactRecord,
    atomic_write_json,
    file_hash,
    git_state,
    read_json,
    records_to_dict,
)
from cmw.core.workflow_graph import (
    ArtifactRequirement,
    CalculationNode,
    WorkflowGraph,
)
from cmw.molecular.multiwfn.adapter import (
    MultiwfnAdapterError,
    MultiwfnCommandSpec,
    MultiwfnOutputSpec,
    build_command_spec,
    discover_outputs,
    normalize_output_paths,
    validate_command_spec,
    validate_output_specs,
)
from cmw.molecular.multiwfn.automation import Operation, menu_stream
from cmw.structure.xyz import read_xyz

from .cube import read_cube, validate_cube_compatibility, validate_cube_geometry
from .esp_cubes import EspConfiguration
from .fmo_cubes import FmoConfiguration, frontier_orbitals
from .igmh import (
    FragmentDefinition,
    IgmhConfiguration,
    load_fragments,
    load_igmh_configuration,
    validate_igmh_execution_contract,
)
from .source import (
    ValidatedSource,
    density_artifact_from_source,
    validate_source_result,
)


ANALYSIS_SCHEMA_VERSION = 1


class AnalysisOperation(str, Enum):
    FMO = "FMO"
    ESP = "ESP"
    IGMH = "IGMH"


DEFAULT_OUTPUTS: dict[AnalysisOperation, tuple[MultiwfnOutputSpec, ...]] = {
    AnalysisOperation.FMO: (
        MultiwfnOutputSpec("homo_cube", "homo.cube", "homo.cube"),
        MultiwfnOutputSpec("lumo_cube", "lumo.cube", "lumo.cube"),
    ),
    AnalysisOperation.ESP: (
        MultiwfnOutputSpec("density_cube", "density.cube", "density.cube"),
        MultiwfnOutputSpec("esp_cube", "esp.cube", "esp.cube"),
    ),
}


def output_contract(
    operation: AnalysisOperation, calculation: Mapping[str, Any]
) -> tuple[MultiwfnOutputSpec, ...]:
    if operation is not AnalysisOperation.IGMH:
        return DEFAULT_OUTPUTS[operation]
    raw = calculation.get("outputs")
    if not isinstance(raw, list):
        raise ValueError("IGMH target lacks an output contract")
    outputs = tuple(
        MultiwfnOutputSpec.from_mapping(item)
        for item in raw
        if isinstance(item, Mapping)
    )
    if len(outputs) != len(raw):
        raise ValueError("IGMH target outputs must be mappings")
    return validate_output_specs(outputs)


def _configuration(
    operation: AnalysisOperation, grid_spacing_bohr: float
) -> FmoConfiguration | EspConfiguration:
    return (
        FmoConfiguration(grid_spacing_bohr)
        if operation is AnalysisOperation.FMO
        else EspConfiguration(grid_spacing_bohr)
    )


def build_target(
    operation: AnalysisOperation,
    source: ValidatedSource,
    *,
    grid_spacing_bohr: float,
    fragments: FragmentDefinition | None = None,
    igmh_configuration: IgmhConfiguration | None = None,
) -> tuple[JobTarget, dict[str, object]]:
    config = (
        igmh_configuration
        if operation is AnalysisOperation.IGMH
        else _configuration(operation, grid_spacing_bohr)
    )
    if config is None:
        raise ValueError("IGMH requires an explicit configuration")
    scientific: dict[str, object] = {
        "operation": operation.value,
        "operation_profile": "multiwfn-3.8",
        "source_target_id": source.target_id,
        "source_wavefunction_sha256": source.wavefunction_sha256,
        "grid_spacing_bohr": config.grid_spacing_bohr,
    }
    if operation is AnalysisOperation.FMO:
        homo, lumo = frontier_orbitals(source)
        scientific.update(
            {
                "spin_mode": "restricted",
                "homo_index": homo,
                "lumo_index": lumo,
                "orbital_indexing": "one_based",
            }
        )
    elif operation is AnalysisOperation.IGMH:
        if fragments is None:
            raise ValueError("IGMH requires an explicit fragment definition")
        scientific.update(fragments.to_dict())
        scientific.update(config.to_dict())
    target = JobTarget(
        stage_type=f"MULTIWFN_{operation.value}",
        geometry_sha256=source.geometry_sha256,
        charge=source.charge,
        multiplicity=source.multiplicity,
        calculation=scientific,
    )
    return target, scientific


def analysis_workflow_graph(
    operation: AnalysisOperation, configuration: Mapping[str, object]
) -> WorkflowGraph:
    """Describe the reusable typed-artifact dependency for one analysis."""

    source_type = (
        "DensityArtifact"
        if operation is AnalysisOperation.IGMH
        else "WavefunctionArtifact"
    )
    output_type = {
        AnalysisOperation.FMO: "AnalysisArtifact",
        AnalysisOperation.ESP: "DensityArtifact",
        AnalysisOperation.IGMH: "IGMHArtifact",
    }[operation]
    analysis_id = f"multiwfn_{operation.value.casefold()}"
    return WorkflowGraph(
        f"{analysis_id}_workflow",
        (
            CalculationNode(
                "source",
                role="validated_external_source",
                produces=(source_type,),
            ),
            CalculationNode(
                analysis_id,
                dependencies=("source",),
                operation="multiwfn_analysis",
                requires=(ArtifactRequirement(source_type, 1, ("source",)),),
                produces=(output_type,),
                configuration=dict(configuration),
            ),
        ),
    )


def target_directory(
    output_root: Path, operation: AnalysisOperation, target_id: str
) -> Path:
    return output_root.resolve() / operation.value.lower() / target_id


def _artifact_path(metadata_path: Path, stored: str) -> Path:
    value = Path(stored)
    return value if value.is_absolute() else metadata_path.parent / value


def check_reuse(
    target: JobTarget,
    result_path: Path,
    source: ValidatedSource,
) -> dict[str, object]:
    try:
        record = read_json(result_path)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return {"reuse": False, "code": "RESULT_MISSING_OR_INVALID", "reason": str(exc)}
    if record.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        return {
            "reuse": False,
            "code": "SCHEMA_MISMATCH",
            "reason": "unsupported result schema",
        }
    if record.get("target", {}).get("target_id") != target.target_id:
        return {
            "reuse": False,
            "code": "TARGET_MISMATCH",
            "reason": "scientific target differs",
        }
    if (
        record.get("source", {}).get("wavefunction_sha256")
        != source.wavefunction_sha256
    ):
        return {
            "reuse": False,
            "code": "SOURCE_MISMATCH",
            "reason": "source wavefunction differs",
        }
    if not record.get("reusable"):
        return {
            "reuse": False,
            "code": "NOT_REUSABLE",
            "reason": "prior result is not reusable",
        }
    typed_artifact = record.get("scientific_artifact")
    if typed_artifact is not None:
        try:
            parsed_artifact = artifact_from_dict(typed_artifact)
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "reuse": False,
                "code": "TYPED_ARTIFACT_INVALID",
                "reason": str(exc),
            }
        if (
            parsed_artifact.producing_calculation != target.target_id
            or not parsed_artifact.validation.passed
        ):
            return {
                "reuse": False,
                "code": "TYPED_ARTIFACT_INVALID",
                "reason": "typed artifact contradicts the reusable analysis result",
            }
        if isinstance(parsed_artifact, IGMHArtifact):
            density_record = record.get("source_density_artifact")
            if not isinstance(density_record, Mapping):
                return {
                    "reuse": False,
                    "code": "DENSITY_LINEAGE_MISSING",
                    "reason": "IGMH result lacks its parent density artifact",
                }
            try:
                density = artifact_from_dict(density_record)
                if not isinstance(density, DensityArtifact):
                    raise ValueError("source artifact is not a DensityArtifact")
                validate_igmh_execution_contract(
                    target.calculation, parsed_artifact, (density,)
                )
            except (TypeError, ValueError) as exc:
                return {
                    "reuse": False,
                    "code": "DENSITY_LINEAGE_INVALID",
                    "reason": str(exc),
                }
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        return {
            "reuse": False,
            "code": "ARTIFACT_MANIFEST_INVALID",
            "reason": "artifact manifest missing",
        }
    paths: dict[str, Path] = {}
    for role in record.get("required_artifact_roles", ()):
        artifact = artifacts.get(role)
        if not isinstance(artifact, dict):
            return {
                "reuse": False,
                "code": "ARTIFACT_MISSING",
                "reason": f"missing {role}",
            }
        path = _artifact_path(result_path, str(artifact.get("path", "")))
        if not path.is_file() or path.stat().st_size != artifact.get("size_bytes"):
            return {
                "reuse": False,
                "code": "ARTIFACT_MISSING",
                "reason": f"missing {role}",
            }
        if file_hash(path) != artifact.get("sha256"):
            return {
                "reuse": False,
                "code": "ARTIFACT_MISMATCH",
                "reason": f"changed {role}",
            }
        paths[role] = path
    try:
        geometry = read_xyz(Path(source.geometry_path))
        command_outputs = record.get("command", {}).get("outputs", [])
        media_types = {
            str(item.get("role")): str(item.get("media_type"))
            for item in command_outputs
            if isinstance(item, Mapping) and item.get("role")
        }
        cubes = {
            role: read_cube(path)
            for role, path in paths.items()
            if media_types.get(role, "application/x-gaussian-cube")
            == "application/x-gaussian-cube"
        }
        for cube in cubes.values():
            validate_cube_geometry(cube, geometry)
        roles = tuple(cubes)
        for role in roles[1:]:
            validate_cube_compatibility(cubes[roles[0]], cubes[role])
    except (OSError, UnicodeError, ValueError) as exc:
        return {"reuse": False, "code": "ARTIFACT_INVALID", "reason": str(exc)}
    return {
        "reuse": True,
        "code": "REUSABLE",
        "reason": "target, source, cube structure, and artifact integrity match",
        "result_path": str(result_path),
        "target_id": target.target_id,
    }


def plan_analysis(
    *,
    operation: AnalysisOperation,
    source_path: Path,
    output_root: Path,
    grid_spacing_bohr: float | None,
    threads: int,
    fragment_path: Path | None = None,
    igmh_config_path: Path | None = None,
) -> dict[str, object]:
    source = validate_source_result(source_path)
    fragments = None
    igmh_configuration = None
    if operation is AnalysisOperation.IGMH:
        if fragment_path is None or igmh_config_path is None:
            raise ValueError("IGMH requires --fragments and --config")
        geometry = read_xyz(Path(source.geometry_path))
        fragments = load_fragments(fragment_path, geometry.atom_count)
        igmh_configuration = load_igmh_configuration(igmh_config_path)
        grid_spacing_bohr = igmh_configuration.grid_spacing_bohr
    elif grid_spacing_bohr is None:
        raise ValueError("--grid-spacing-bohr is required for FMO and ESP")
    assert grid_spacing_bohr is not None
    target, configuration = build_target(
        operation,
        source,
        grid_spacing_bohr=grid_spacing_bohr,
        fragments=fragments,
        igmh_configuration=igmh_configuration,
    )
    outputs = output_contract(operation, configuration)
    density_artifact = (
        density_artifact_from_source(source)
        if operation is AnalysisOperation.IGMH
        else None
    )
    directory = target_directory(output_root, operation, target.target_id)
    result_path = directory / "result.json"
    reuse = check_reuse(target, result_path, source)
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "operation": operation.value,
        "source": source.to_dict(),
        "output_root": str(output_root.resolve()),
        "target_id": target.target_id,
        "target_path": str(directory / "target.json"),
        "result_path": str(result_path),
        "configuration": configuration,
        "workflow_graph": analysis_workflow_graph(operation, configuration).to_dict(),
        "output_contract": [item.to_dict() for item in outputs],
        "visualization": dict(
            igmh_configuration.visualization if igmh_configuration is not None else {}
        ),
        "source_density_artifact": (
            density_artifact.to_dict() if density_artifact is not None else None
        ),
        "runtime": {"requested_nthreads": threads},
        "reuse": reuse,
        "expected_artifacts": [item.output_path for item in outputs if item.required],
    }


def prepare_analysis(plan: Mapping[str, Any]) -> dict[str, object]:
    target_path = Path(str(plan["target_path"]))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_record = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "target": {
            "stage_type": f"MULTIWFN_{plan['operation']}",
            "geometry_sha256": plan["source"]["geometry_sha256"],
            "charge": plan["source"]["charge"],
            "multiplicity": plan["source"]["multiplicity"],
            "calculation": plan["configuration"],
            "target_id": plan["target_id"],
        },
        "source": plan["source"],
    }
    if plan.get("source_density_artifact") is not None:
        target_record["source_density_artifact"] = plan["source_density_artifact"]
    if target_path.exists():
        existing = read_json(target_path)
        if existing != target_record:
            raise FileExistsError(
                "stored target content conflicts with target identity"
            )
    else:
        atomic_write_json(target_path, target_record)
    attempt_id = uuid4().hex
    attempt_directory = target_path.parent / "attempts" / attempt_id
    attempt_directory.mkdir(parents=True)
    atomic_write_json(attempt_directory / "analysis-plan.json", dict(plan))
    return {
        **dict(plan),
        "attempt_id": attempt_id,
        "attempt_directory": str(attempt_directory),
    }


def write_menu(
    *, target_path: Path, runtime_path: Path, destination: Path
) -> dict[str, object]:
    target_record = read_json(target_path)
    runtime = read_json(runtime_path)["runtime"]
    calculation = target_record["target"]["calculation"]
    operation = AnalysisOperation(str(calculation["operation"]))
    parameters = dict(calculation)
    automation_operation = {
        AnalysisOperation.FMO: Operation.FMO,
        AnalysisOperation.ESP: Operation.ESP,
        AnalysisOperation.IGMH: Operation.IGMH,
    }[operation]
    text = menu_stream(automation_operation, str(runtime["version"]), parameters)
    destination.write_text(text, encoding="utf-8")
    outputs = output_contract(operation, calculation)
    command = build_command_spec(
        runtime=runtime,
        source_path=Path(str(target_record["source"]["wavefunction_path"])),
        attempt_directory=destination.parent,
        stdin_path=destination,
        outputs=outputs,
    )
    command_path = destination.parent / "command.json"
    atomic_write_json(command_path, command.to_dict())
    return {
        "menu_path": str(destination),
        "command_path": str(command_path),
        "operation": operation.value,
        "command": command.to_dict(),
    }


def normalize_outputs(*, target_path: Path, attempt_directory: Path) -> dict[str, str]:
    """Normalize raw Multiwfn files through the declared output contract."""

    target = read_json(target_path)["target"]
    calculation = target["calculation"]
    operation = AnalysisOperation(str(calculation["operation"]))
    if operation is AnalysisOperation.FMO:
        outputs = (
            MultiwfnOutputSpec(
                "homo_cube",
                f"orb{int(calculation['homo_index']):06d}.cub",
                "homo.cube",
            ),
            MultiwfnOutputSpec(
                "lumo_cube",
                f"orb{int(calculation['lumo_index']):06d}.cub",
                "lumo.cube",
            ),
        )
    elif operation is AnalysisOperation.ESP:
        outputs = (
            MultiwfnOutputSpec("density_cube", "density.cub", "density.cube"),
            MultiwfnOutputSpec("esp_cube", "totesp.cub", "esp.cube"),
        )
    else:
        outputs = output_contract(operation, calculation)
    return normalize_output_paths(attempt_directory, outputs)


def _scientific_artifact(
    record: Mapping[str, Any],
    density: DensityArtifact | None,
    *,
    metadata_path: Path,
    artifact_root: Path,
) -> Artifact:
    artifact = artifact_from_result(record)
    if not isinstance(artifact, IGMHArtifact):
        return artifact
    if density is None:
        raise ValueError("IGMH result requires a DensityArtifact parent")
    runtime = dict(record.get("runtime", {}))
    target = dict(record["target"])
    calculation = dict(target["calculation"])
    manifest = dict(record.get("artifacts", {}))
    command_outputs = record.get("command", {}).get("outputs", [])
    declared_output_roles = {
        str(item.get("role"))
        for item in command_outputs
        if isinstance(item, Mapping) and item.get("role")
    }
    relative_metadata = metadata_path.relative_to(artifact_root)
    artifact_files = {**dict(artifact.files), "metadata": str(relative_metadata)}
    input_roles = {
        "source_wavefunction",
        "source_geometry",
        "automation_input",
        "analysis_plan",
        "command_spec",
        "runtime_metadata",
    }
    igmh = IGMHArtifact(
        producing_calculation=str(target["target_id"]),
        method=density.method,
        basis=density.basis,
        protocol=calculation,
        parent_artifacts=(density.artifact_id,),
        files=artifact_files,
        validation=artifact.validation,
        provenance={
            **dict(artifact.provenance),
            "generating_program": "Multiwfn",
            "multiwfn_version": runtime.get("version"),
            "runtime": runtime,
            "input_files": {
                role: item.get("path")
                for role, item in manifest.items()
                if role in input_roles and isinstance(item, Mapping)
            },
            "output_files": {
                role: item.get("path")
                for role, item in manifest.items()
                if role in declared_output_roles and isinstance(item, Mapping)
            }
            | {"metadata": str(relative_metadata)},
            "visualization": dict(record.get("visualization", {})),
        },
        metadata={
            **dict(artifact.metadata),
            "analysis_protocol": calculation,
            "density_source": density.artifact_id,
            "grid_spacing_bohr": calculation.get("grid_spacing_bohr"),
            "multiwfn_protocol": calculation,
            "visualization": dict(record.get("visualization", {})),
        },
    )
    validate_igmh_execution_contract(calculation, igmh, (density,))
    return igmh


def finalize_analysis(
    *,
    target_path: Path,
    runtime_path: Path,
    menu_path: Path,
    attempt_directory: Path,
    process_exit_code: int,
    repository: Path | None = None,
) -> dict[str, object]:
    target_record = read_json(target_path)
    runtime_record = read_json(runtime_path)
    source = validate_source_result(Path(target_record["source"]["result_path"]))
    target_data = target_record["target"]
    target = JobTarget(
        stage_type=str(target_data["stage_type"]),
        geometry_sha256=str(target_data["geometry_sha256"]),
        charge=int(target_data["charge"]),
        multiplicity=int(target_data["multiplicity"]),
        calculation=dict(target_data["calculation"]),
    )
    if target.target_id != target_data.get("target_id"):
        raise ValueError("stored downstream target identity is invalid")
    operation = AnalysisOperation(str(target.calculation["operation"]))
    if process_exit_code != 0:
        raise MultiwfnAdapterError(
            "PROCESS_EXIT_NONZERO",
            f"Multiwfn exited with status {process_exit_code}",
            details={"process_exit_code": process_exit_code},
        )
    runtime = runtime_record["runtime"]
    command_path = attempt_directory / "command.json"
    command = MultiwfnCommandSpec.from_mapping(read_json(command_path))
    if command.runtime != runtime:
        raise MultiwfnAdapterError(
            "COMMAND_PROVENANCE_MISMATCH",
            "stored command runtime differs from runtime.json",
        )
    validate_command_spec(command)
    outputs = output_contract(operation, target.calculation)
    discovered = discover_outputs(attempt_directory, outputs)
    geometry = read_xyz(Path(source.geometry_path))
    records: dict[str, ArtifactRecord] = {}
    cubes = {}
    output_map = {item.role: item for item in outputs}
    for role, path in discovered.items():
        if output_map[role].media_type == "application/x-gaussian-cube":
            cube = read_cube(path)
            validate_cube_geometry(cube, geometry)
            cubes[role] = cube
        records[role] = ArtifactRecord.from_path(
            path, role=role, relative_to=target_path.parent
        )
    for role, filename in {
        "automation_input": "menu.in",
        "analysis_plan": "analysis-plan.json",
        "command_spec": "command.json",
        "multiwfn_log": "multiwfn.log",
        "multiwfn_stderr": "multiwfn.stderr",
        "runtime_metadata": "runtime.json",
        "health_diagnostic": "health-latest.json",
        "health_history": "health-history.jsonl",
        "temporary_alias_record": "multiwfn-runtime-alias.txt",
    }.items():
        path = attempt_directory / filename
        if path.is_file():
            records[role] = ArtifactRecord.from_path(
                path, role=role, relative_to=target_path.parent
            )
    for role, path in {
        "source_wavefunction": Path(source.wavefunction_path),
        "source_geometry": Path(source.geometry_path),
    }.items():
        records[role] = ArtifactRecord.from_path(
            path, role=role, relative_to=target_path.parent
        )
    cube_roles = tuple(cubes)
    for role in cube_roles[1:]:
        validate_cube_compatibility(cubes[cube_roles[0]], cubes[role])
    roles = tuple(item.role for item in outputs if item.required)
    attempt = ExecutionAttempt.create(
        target_id=target.target_id,
        resources={"multiwfn_nthreads": runtime["requested_nthreads"]},
        executable={
            "path": runtime["executable"],
            "sha256": runtime["executable_sha256"],
            "version": runtime["version"],
        },
        generated_input_sha256=file_hash(menu_path),
    )
    plan_record = read_json(attempt_directory / "analysis-plan.json")
    density = None
    density_record = target_record.get("source_density_artifact")
    if density_record is not None:
        if not isinstance(density_record, Mapping):
            raise ValueError("source_density_artifact must be a mapping")
        parsed_density = artifact_from_dict(density_record)
        if not isinstance(parsed_density, DensityArtifact):
            raise ValueError("IGMH source artifact must be a DensityArtifact")
        density = parsed_density
    attempt_record = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "target": target.to_dict(),
        "source": source.to_dict(),
        "attempt": attempt.to_dict(),
        "runtime": runtime,
        "command": command.to_dict(),
        "execution": {
            "status": "SUCCESS",
            "process_exit_code": process_exit_code,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
        "scientific": {
            "status": "VALID",
            "reason": "declared outputs, source geometry, and cube grids validated",
        },
        "artifacts": records_to_dict(records),
        "required_artifact_roles": list(roles),
        "source_density_artifact": (density.to_dict() if density is not None else None),
        "parent_artifacts": ([density.artifact_id] if density is not None else []),
        "workflow_graph": plan_record.get("workflow_graph"),
        "visualization": dict(plan_record.get("visualization", {})),
        "provenance": {"git": git_state(repository)},
        "reusable": True,
    }
    attempt_record["scientific_artifact"] = _scientific_artifact(
        attempt_record,
        density,
        metadata_path=attempt_directory / "attempt.json",
        artifact_root=target_path.parent,
    ).to_dict()
    atomic_write_json(attempt_directory / "attempt.json", attempt_record)
    result_path = target_path.parent / "result.json"
    atomic_write_json(result_path, attempt_record)
    return {**attempt_record, "result_path": str(result_path)}
