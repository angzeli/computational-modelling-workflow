"""Shared target, attempt, artifact, and reuse semantics for Multiwfn analyses."""

from __future__ import annotations

from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from cmw.core.job import ExecutionAttempt, JobTarget
from cmw.core.provenance import (
    ArtifactRecord,
    atomic_write_json,
    file_hash,
    git_state,
    read_json,
    records_to_dict,
)
from cmw.molecular.multiwfn.automation import Operation, menu_stream
from cmw.structure.xyz import read_xyz

from .cube import read_cube, validate_cube_compatibility, validate_cube_geometry
from .esp_cubes import EspConfiguration
from .fmo_cubes import FmoConfiguration, frontier_orbitals
from .source import ValidatedSource, validate_source_result


ANALYSIS_SCHEMA_VERSION = 1


class AnalysisOperation(str, Enum):
    FMO = "FMO"
    ESP = "ESP"


EXPECTED_ARTIFACTS: dict[AnalysisOperation, tuple[str, ...]] = {
    AnalysisOperation.FMO: ("homo_cube", "lumo_cube"),
    AnalysisOperation.ESP: ("density_cube", "esp_cube"),
}
ARTIFACT_FILENAMES = {
    "homo_cube": "homo.cube",
    "lumo_cube": "lumo.cube",
    "density_cube": "density.cube",
    "esp_cube": "esp.cube",
}


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
) -> tuple[JobTarget, dict[str, object]]:
    config = _configuration(operation, grid_spacing_bohr)
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
    target = JobTarget(
        stage_type=f"MULTIWFN_{operation.value}",
        geometry_sha256=source.geometry_sha256,
        charge=source.charge,
        multiplicity=source.multiplicity,
        calculation=scientific,
    )
    return target, scientific


def target_directory(output_root: Path, operation: AnalysisOperation, target_id: str) -> Path:
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
        return {"reuse": False, "code": "SCHEMA_MISMATCH", "reason": "unsupported result schema"}
    if record.get("target", {}).get("target_id") != target.target_id:
        return {"reuse": False, "code": "TARGET_MISMATCH", "reason": "scientific target differs"}
    if record.get("source", {}).get("wavefunction_sha256") != source.wavefunction_sha256:
        return {"reuse": False, "code": "SOURCE_MISMATCH", "reason": "source wavefunction differs"}
    if not record.get("reusable"):
        return {"reuse": False, "code": "NOT_REUSABLE", "reason": "prior result is not reusable"}
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        return {"reuse": False, "code": "ARTIFACT_MANIFEST_INVALID", "reason": "artifact manifest missing"}
    paths: dict[str, Path] = {}
    for role in record.get("required_artifact_roles", ()):
        artifact = artifacts.get(role)
        if not isinstance(artifact, dict):
            return {"reuse": False, "code": "ARTIFACT_MISSING", "reason": f"missing {role}"}
        path = _artifact_path(result_path, str(artifact.get("path", "")))
        if not path.is_file() or path.stat().st_size != artifact.get("size_bytes"):
            return {"reuse": False, "code": "ARTIFACT_MISSING", "reason": f"missing {role}"}
        if file_hash(path) != artifact.get("sha256"):
            return {"reuse": False, "code": "ARTIFACT_MISMATCH", "reason": f"changed {role}"}
        paths[role] = path
    try:
        geometry = read_xyz(Path(source.geometry_path))
        cubes = {role: read_cube(path) for role, path in paths.items()}
        for cube in cubes.values():
            validate_cube_geometry(cube, geometry)
        roles = tuple(paths)
        if len(roles) == 2:
            validate_cube_compatibility(cubes[roles[0]], cubes[roles[1]])
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
    grid_spacing_bohr: float,
    threads: int,
) -> dict[str, object]:
    source = validate_source_result(source_path)
    target, configuration = build_target(
        operation, source, grid_spacing_bohr=grid_spacing_bohr
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
        "runtime": {"requested_nthreads": threads},
        "reuse": reuse,
        "expected_artifacts": [ARTIFACT_FILENAMES[role] for role in EXPECTED_ARTIFACTS[operation]],
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
    if target_path.exists():
        existing = read_json(target_path)
        if existing != target_record:
            raise FileExistsError("stored target content conflicts with target identity")
    else:
        atomic_write_json(target_path, target_record)
    attempt_id = uuid4().hex
    attempt_directory = target_path.parent / "attempts" / attempt_id
    attempt_directory.mkdir(parents=True)
    return {**dict(plan), "attempt_id": attempt_id, "attempt_directory": str(attempt_directory)}


def write_menu(
    *, target_path: Path, runtime_path: Path, destination: Path
) -> dict[str, object]:
    target_record = read_json(target_path)
    runtime = read_json(runtime_path)["runtime"]
    calculation = target_record["target"]["calculation"]
    operation = AnalysisOperation(str(calculation["operation"]))
    parameters = dict(calculation)
    automation_operation = Operation.FMO if operation is AnalysisOperation.FMO else Operation.ESP
    text = menu_stream(automation_operation, str(runtime["version"]), parameters)
    destination.write_text(text, encoding="utf-8")
    return {"menu_path": str(destination), "operation": operation.value}


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
        raise ValueError(f"Multiwfn exited with status {process_exit_code}")
    geometry = read_xyz(Path(source.geometry_path))
    records: dict[str, ArtifactRecord] = {}
    cubes = {}
    for role in EXPECTED_ARTIFACTS[operation]:
        path = attempt_directory / ARTIFACT_FILENAMES[role]
        if not path.is_file():
            raise ValueError(f"Multiwfn did not produce required artifact: {path.name}")
        cube = read_cube(path)
        validate_cube_geometry(cube, geometry)
        cubes[role] = cube
        records[role] = ArtifactRecord.from_path(
            path, role=role, relative_to=target_path.parent
        )
    roles = EXPECTED_ARTIFACTS[operation]
    validate_cube_compatibility(cubes[roles[0]], cubes[roles[1]])
    runtime = runtime_record["runtime"]
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
    attempt_record = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "target": target.to_dict(),
        "source": source.to_dict(),
        "attempt": attempt.to_dict(),
        "runtime": runtime,
        "execution": {"status": "SUCCESS", "process_exit_code": process_exit_code},
        "scientific": {"status": "VALID", "reason": "required compatible cubes validated"},
        "artifacts": records_to_dict(records),
        "required_artifact_roles": list(roles),
        "provenance": {"git": git_state(repository)},
        "reusable": True,
    }
    atomic_write_json(attempt_directory / "attempt.json", attempt_record)
    result_path = target_path.parent / "result.json"
    atomic_write_json(result_path, attempt_record)
    return {**attempt_record, "result_path": str(result_path)}
