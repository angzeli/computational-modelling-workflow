"""ORCA target preparation, attempt finalization, and fail-closed reuse."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from cmw.core.artifacts import artifact_from_dict, artifact_from_result
from cmw.core.execution_layout import ExecutionLayout, ExecutionLayoutError
from cmw.core.job import ExecutionAttempt, GeometryLineage, JobTarget
from cmw.core.structure_artifacts import structure_artifact_path
from cmw.core.provenance import (
    ArtifactRecord,
    atomic_write_json,
    file_hash,
    git_state,
    read_json,
    records_to_dict,
)
from cmw.structure.xyz import read_xyz

from .status import (
    ExecutionStatus,
    FrequencyPolicy,
    ScientificStatus,
    StageType,
    classify_execution,
    read_orca_output,
    validate_stage,
)
from .input import orca_execution_intent, parse_rendered_orca_input
from .geometry import (
    OrcaGeometryInput,
    OrcaGeometryMode,
    legacy_geometry_provenance,
    validate_orca_geometry_input,
)
from .protocol import ProtocolIntent, validate_protocol


ORCA_JOB_SCHEMA_VERSION = 1


def write_target(
    path: Path,
    target: JobTarget,
    lineage: GeometryLineage,
) -> None:
    """Persist scientific intent independently of any execution attempt."""

    if target.geometry_sha256 != lineage.geometry_sha256:
        raise ValueError("target geometry and lineage geometry identities differ")
    atomic_write_json(
        path,
        {
            "schema_version": ORCA_JOB_SCHEMA_VERSION,
            "target": target.to_dict(),
            "lineage": asdict(lineage),
        },
    )


def _target_from_record(record: Mapping[str, Any]) -> JobTarget:
    target = record["target"]
    parsed = JobTarget(
        stage_type=str(target["stage_type"]),
        geometry_sha256=str(target["geometry_sha256"]),
        charge=int(target["charge"]),
        multiplicity=int(target["multiplicity"]),
        calculation=dict(target["calculation"]),
    )
    if target.get("target_id") != parsed.target_id:
        raise ValueError("stored target identity does not match target content")
    return parsed


def load_target(path: Path) -> tuple[JobTarget, dict[str, Any]]:
    record = read_json(path)
    if record.get("schema_version") != ORCA_JOB_SCHEMA_VERSION:
        raise ValueError("unsupported ORCA target schema")
    return _target_from_record(record), record


def _artifact_path(metadata_path: Path, stored_path: str) -> Path:
    path = Path(stored_path)
    return path if path.is_absolute() else metadata_path.parent / path


def finalize_attempt(
    *,
    target_path: Path,
    metadata_path: Path,
    input_path: Path,
    output_path: Path,
    stderr_path: Path,
    process_exit_code: int,
    executable: Mapping[str, object],
    resources: Mapping[str, object],
    artifacts: Mapping[str, Path] | None = None,
    repository: Path | None = None,
    frequency_policy: FrequencyPolicy = FrequencyPolicy(),
    parent_attempt_id: str | None = None,
    execution_layout: ExecutionLayout | None = None,
    geometry_input: OrcaGeometryInput | None = None,
    geometry_contract_path: Path | None = None,
) -> dict[str, Any]:
    """Validate and atomically record one immutable ORCA attempt."""

    target, target_record = load_target(target_path)
    supplied_artifacts = dict(artifacts or {})
    if geometry_input is not None:
        validate_orca_geometry_input(geometry_input)
        if geometry_contract_path is None or not geometry_contract_path.is_file():
            raise ValueError("artifact-backed ORCA execution requires geometry contract metadata")
    elif geometry_contract_path is not None:
        raise ValueError("geometry contract metadata requires an artifact-backed input")
    if execution_layout is not None:
        execution_layout.validate_paths(
            input_paths=(
                input_path,
                *((geometry_input.input_path,) if geometry_input is not None else ()),
            ),
            output_paths=(output_path, *supplied_artifacts.values()),
            metadata_paths=(
                metadata_path,
                *((geometry_contract_path,) if geometry_contract_path is not None else ()),
            ),
            log_paths=(stderr_path,),
        )
        if target.target_id != execution_layout.target_identifier:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: layout target identity conflicts "
                "with the scientific target"
            )
        expected_paths = {
            "target": execution_layout.target_directory / "target.json",
            "input": execution_layout.input_path("stage.inp"),
            "output": execution_layout.output_path("stage.out"),
            "stderr": execution_layout.log_path("stage.err"),
            "metadata": execution_layout.metadata_path("job.json"),
            "geometry_contract": execution_layout.metadata_path("geometry-input.json"),
            "input_geometry": execution_layout.input_path("input.xyz"),
        }
        actual_paths = {
            "target": target_path,
            "input": input_path,
            "output": output_path,
            "stderr": stderr_path,
            "metadata": metadata_path,
            "geometry_contract": geometry_contract_path,
            "input_geometry": (
                geometry_input.input_path if geometry_input is not None else None
            ),
        }
        conflicts = [
            name
            for name, expected in expected_paths.items()
            if actual_paths[name] is not None and actual_paths[name].resolve() != expected
        ]
        if conflicts:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: non-deterministic ORCA paths: "
                + ", ".join(conflicts)
            )
    actual_input_hash = file_hash(input_path)
    stage_type = StageType(target.stage_type)
    parsed_target, parsed_resources = parse_rendered_orca_input(
        input_path,
        stage_type,
        geometry_input=geometry_input,
    )
    expected_calculation = dict(target.calculation)
    expected_calculation.pop("validation_policy", None)
    expected_calculation.pop("protocol", None)
    if (
        parsed_target.stage_type != target.stage_type
        or parsed_target.geometry_sha256 != target.geometry_sha256
        or parsed_target.charge != target.charge
        or parsed_target.multiplicity != target.multiplicity
        or dict(parsed_target.calculation) != expected_calculation
    ):
        raise ValueError("generated ORCA input does not match its scientific target")
    if dict(resources) != parsed_resources.to_dict():
        raise ValueError("reported attempt resources contradict the generated ORCA input")
    evidence = read_orca_output(output_path, stderr_path=stderr_path)
    execution = classify_execution(evidence, process_exit_code=process_exit_code)
    scientific = validate_stage(
        evidence,
        stage_type,
        execution=execution,
        frequency_policy=frequency_policy,
    )
    protocol_intent = ProtocolIntent.from_mapping(
        target.calculation.get("protocol")
        if isinstance(target.calculation.get("protocol"), Mapping)
        else None
    )
    protocol_validation = validate_protocol(evidence, protocol_intent, stage_type)
    if target.calculation.get("protocol") and not protocol_validation.valid:
        scientific = type(scientific)(
            ScientificStatus.INVALID,
            stage_type,
            protocol_validation.reason,
            scientific.imaginary_frequencies_cm1,
            scientific.significant_imaginary_frequencies_cm1,
        )
    attempt = ExecutionAttempt.create(
        target_id=target.target_id,
        resources=resources,
        executable=executable,
        generated_input_sha256=actual_input_hash,
        parent_attempt_id=parent_attempt_id,
        attempt_id=(
            execution_layout.attempt_identifier
            if execution_layout is not None
            else None
        ),
    )

    selected_artifacts: dict[str, ArtifactRecord] = {}
    geometry_artifacts: dict[str, Path] = {}
    if geometry_input is not None:
        source_structure = structure_artifact_path(geometry_input.source_artifact)
        if source_structure is None:
            raise ValueError("StructureArtifact geometry file is missing")
        geometry_artifacts = {
            "input_geometry": geometry_input.input_path,
            "source_structure": source_structure,
            "geometry_contract": geometry_contract_path,
        }
    for role, path in {
        "input": input_path,
        "output": output_path,
        "stderr": stderr_path,
        **geometry_artifacts,
        **supplied_artifacts,
    }.items():
        if path.is_file():
            selected_artifacts[role] = ArtifactRecord.from_path(
                path,
                role=role,
                relative_to=(
                    execution_layout.working_directory
                    if execution_layout is not None
                    else metadata_path.parent
                ),
            )
    required_roles = ["input", "output"]
    if geometry_input is not None:
        required_roles.extend(
            ("input_geometry", "source_structure", "geometry_contract")
        )
    if stage_type is StageType.OPT:
        required_roles.append("final_geometry")
    missing_roles = [role for role in required_roles if role not in selected_artifacts]
    if stage_type is StageType.OPT and "final_geometry" in selected_artifacts:
        try:
            read_xyz(supplied_artifacts["final_geometry"])
        except (OSError, UnicodeError, ValueError) as exc:
            missing_roles.append("valid_final_geometry")
            if scientific.status is ScientificStatus.VALID:
                scientific = type(scientific)(
                    ScientificStatus.INVALID,
                    stage_type,
                    f"final geometry is not a valid XYZ structure: {exc}",
                    scientific.imaginary_frequencies_cm1,
                    scientific.significant_imaginary_frequencies_cm1,
                )
    if missing_roles and scientific.status is ScientificStatus.VALID:
        scientific = type(scientific)(
            ScientificStatus.INVALID,
            stage_type,
            f"required artifacts are missing or invalid: {', '.join(missing_roles)}",
            scientific.imaginary_frequencies_cm1,
            scientific.significant_imaginary_frequencies_cm1,
        )

    if geometry_input is not None:
        geometry_record = geometry_input.to_dict()
        parent_artifacts = [geometry_input.source_artifact.artifact_id]
    else:
        geometry_reference = Path(input_path.read_text(encoding="utf-8").splitlines()[-1].split()[-1])
        geometry_record = legacy_geometry_provenance(
            input_path.parent / geometry_reference,
            mode=OrcaGeometryMode.LEGACY_XYZFILE,
        )
        parent_artifacts = []
    record = {
        "schema_version": ORCA_JOB_SCHEMA_VERSION,
        "target": target.to_dict(),
        "execution_intent": orca_execution_intent(stage_type).to_dict(),
        "execution_layout": (
            execution_layout.to_dict() if execution_layout is not None else None
        ),
        "geometry_input": geometry_record,
        "geometry_contract_path": (
            str(geometry_contract_path.resolve())
            if geometry_contract_path is not None
            else None
        ),
        "parent_artifacts": parent_artifacts,
        "lineage": target_record["lineage"],
        "attempt": attempt.to_dict(),
        "execution": {**asdict(execution), "status": execution.status.value},
        "scientific": {**asdict(scientific), "status": scientific.status.value, "stage_type": stage_type.value},
        "validation": protocol_validation.to_dict(),
        "evidence": evidence.to_dict(),
        "artifacts": records_to_dict(selected_artifacts),
        "required_artifact_roles": required_roles,
        "provenance": {"git": git_state(repository), "executable": dict(executable)},
        "reusable": execution.status is ExecutionStatus.SUCCESS
        and scientific.status is ScientificStatus.VALID
        and not missing_roles,
    }
    record["scientific_artifact"] = artifact_from_result(record).to_dict()
    atomic_write_json(metadata_path, record)
    return record


def check_reuse(target_path: Path, metadata_path: Path) -> dict[str, Any]:
    """Reuse only exact target, versioned metadata, valid science, and intact artifacts."""

    try:
        target, _ = load_target(target_path)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return {"reuse": False, "code": "TARGET_INVALID", "reason": str(exc)}
    try:
        record = read_json(metadata_path)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return {"reuse": False, "code": "METADATA_MISSING_OR_INVALID", "reason": str(exc)}
    if record.get("schema_version") != ORCA_JOB_SCHEMA_VERSION:
        return {"reuse": False, "code": "SCHEMA_MISMATCH", "reason": "unsupported metadata schema"}
    if record.get("target", {}).get("target_id") != target.target_id:
        return {"reuse": False, "code": "TARGET_MISMATCH", "reason": "scientific target identity differs"}
    geometry_record = record.get("geometry_input")
    if isinstance(geometry_record, Mapping) and geometry_record.get("mode") == OrcaGeometryMode.XYZFILE.value:
        try:
            geometry_input = OrcaGeometryInput.from_mapping(geometry_record)
            validate_orca_geometry_input(geometry_input)
            if geometry_input.geometry_hash != target.geometry_sha256:
                raise ValueError("geometry contract hash differs from target geometry")
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return {
                "reuse": False,
                "code": "GEOMETRY_INPUT_INVALID",
                "reason": str(exc),
            }
    layout = None
    layout_record = record.get("execution_layout")
    if layout_record is not None:
        try:
            if not isinstance(layout_record, Mapping):
                raise ExecutionLayoutError("execution layout must be a mapping")
            layout = ExecutionLayout.from_mapping(layout_record)
            layout.validate(require_existing=True)
            layout.validate_attempt_identity(
                str(record.get("attempt", {}).get("attempt_id", ""))
            )
            if layout.target_identifier != target.target_id:
                raise ExecutionLayoutError("layout target identity differs")
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "reuse": False,
                "code": "LAYOUT_INVALID",
                "reason": str(exc),
            }
    if not record.get("reusable"):
        return {"reuse": False, "code": "SCIENTIFICALLY_INVALID", "reason": "prior attempt is not scientifically valid"}
    if record.get("execution", {}).get("status") != ExecutionStatus.SUCCESS.value:
        return {"reuse": False, "code": "EXECUTION_INVALID", "reason": "prior execution was not successful"}
    if record.get("scientific", {}).get("status") != ScientificStatus.VALID.value:
        return {"reuse": False, "code": "SCIENTIFICALLY_INVALID", "reason": "prior scientific status is not valid"}
    typed_artifact = record.get("scientific_artifact")
    if typed_artifact is not None:
        try:
            parsed_artifact = artifact_from_dict(typed_artifact)
        except (KeyError, TypeError, ValueError) as exc:
            return {"reuse": False, "code": "TYPED_ARTIFACT_INVALID", "reason": str(exc)}
        if (
            parsed_artifact.producing_calculation != target.target_id
            or not parsed_artifact.validation.passed
        ):
            return {
                "reuse": False,
                "code": "TYPED_ARTIFACT_INVALID",
                "reason": "typed artifact contradicts the reusable calculation result",
            }

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        return {"reuse": False, "code": "ARTIFACT_MANIFEST_INVALID", "reason": "artifact manifest is missing"}
    for role in record.get("required_artifact_roles", ()):
        artifact = artifacts.get(role)
        if not isinstance(artifact, dict):
            return {"reuse": False, "code": "ARTIFACT_MISSING", "reason": f"required artifact is absent: {role}"}
        stored_path = Path(str(artifact.get("path", "")))
        path = (
            stored_path
            if stored_path.is_absolute()
            else (layout.working_directory / stored_path)
            if layout is not None
            else _artifact_path(metadata_path, str(stored_path))
        )
        if not path.is_file():
            return {"reuse": False, "code": "ARTIFACT_MISSING", "reason": f"required artifact is absent: {role}"}
        if path.stat().st_size != artifact.get("size_bytes") or file_hash(path) != artifact.get("sha256"):
            return {"reuse": False, "code": "ARTIFACT_MISMATCH", "reason": f"artifact integrity mismatch: {role}"}
    return {
        "reuse": True,
        "code": "REUSABLE",
        "reason": "target identity, scientific validity, and artifact integrity match",
        "metadata": str(metadata_path),
        "attempt_id": record.get("attempt", {}).get("attempt_id"),
        "target_id": target.target_id,
    }
