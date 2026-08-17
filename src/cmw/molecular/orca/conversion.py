"""Validated ORCA wavefunction conversion for downstream analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Mapping

from cmw.core.execution_layout import ExecutionLayout, next_attempt_identifier
from cmw.core.job import JobTarget
from cmw.core.provenance import (
    ArtifactRecord,
    atomic_write_json,
    file_hash,
    read_json,
    records_to_dict,
    stable_hash,
)
from cmw.structure.xyz import geometry_hash, read_xyz


CONVERSION_SCHEMA_VERSION = 1


class OrcaWavefunctionConversionError(ValueError):
    """Raised when an ORCA wavefunction cannot be converted reproducibly."""

    code = "FAILED_WAVEFUNCTION_CONVERSION"


def _failure(reason: str) -> OrcaWavefunctionConversionError:
    return OrcaWavefunctionConversionError(
        f"{OrcaWavefunctionConversionError.code}: {reason}"
    )


def _artifact_path(result_path: Path, artifact: Mapping[str, Any]) -> Path:
    path = Path(str(artifact.get("path", "")))
    return path if path.is_absolute() else result_path.parent / path


def _validated_source_artifact(
    result_path: Path, artifacts: Mapping[str, Any], role: str
) -> Path:
    value = artifacts.get(role)
    if not isinstance(value, Mapping):
        raise _failure(f"source result lacks required artifact role: {role}")
    path = _artifact_path(result_path, value).resolve()
    if not path.is_file():
        raise _failure(f"source artifact is missing: {role}: {path}")
    if (
        value.get("sha256") != file_hash(path)
        or value.get("size_bytes") != path.stat().st_size
    ):
        raise _failure(f"source artifact integrity mismatch: {role}")
    return path


@dataclass(frozen=True)
class MoldenConversionPlan:
    """One deterministic GBW-to-Molden conversion attempt."""

    source_result: Path
    source_target_id: str
    source_wavefunction: Path
    source_wavefunction_sha256: str
    source_geometry: Path
    geometry_sha256: str
    charge: int
    multiplicity: int
    spin_mode: str
    converter: Path
    converter_sha256: str
    output_root: Path
    target: JobTarget
    target_path: Path
    result_path: Path
    reusable: bool
    reusable_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": CONVERSION_SCHEMA_VERSION,
            "operation": "orca_gbw_to_molden",
            "source_result": str(self.source_result),
            "source_target_id": self.source_target_id,
            "source_wavefunction": str(self.source_wavefunction),
            "source_wavefunction_sha256": self.source_wavefunction_sha256,
            "source_geometry": str(self.source_geometry),
            "geometry_sha256": self.geometry_sha256,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "spin_mode": self.spin_mode,
            "converter": str(self.converter),
            "converter_sha256": self.converter_sha256,
            "output_root": str(self.output_root),
            "target": self.target.to_dict(),
            "target_path": str(self.target_path),
            "result_path": str(self.result_path),
            "reuse": {
                "reuse": self.reusable,
                "reason": self.reusable_reason,
            },
        }


def _source_contract(
    source_result: Path,
) -> tuple[dict[str, Any], JobTarget, Path, Path]:
    result_path = source_result.expanduser().resolve(strict=True)
    record = read_json(result_path)
    if record.get("schema_version") != 1:
        raise _failure("unsupported source-result schema")
    if (
        not record.get("reusable")
        or record.get("execution", {}).get("status") != "SUCCESS"
        or record.get("scientific", {}).get("status") != "VALID"
    ):
        raise _failure("source ORCA result is not reusable and scientifically valid")
    raw_target = record.get("target")
    artifacts = record.get("artifacts")
    if not isinstance(raw_target, Mapping) or not isinstance(artifacts, Mapping):
        raise _failure("source target or artifact manifest is invalid")
    target = JobTarget(
        stage_type=str(raw_target["stage_type"]),
        geometry_sha256=str(raw_target["geometry_sha256"]),
        charge=int(raw_target["charge"]),
        multiplicity=int(raw_target["multiplicity"]),
        calculation=dict(raw_target["calculation"]),
    )
    if target.target_id != raw_target.get("target_id"):
        raise _failure("source target identity does not match its content")
    wavefunction = _validated_source_artifact(result_path, artifacts, "wavefunction")
    if wavefunction.suffix.casefold() != ".gbw":
        raise _failure("source wavefunction must be an ORCA GBW file")
    geometry_role = "input_geometry" if "input_geometry" in artifacts else "geometry"
    geometry = _validated_source_artifact(result_path, artifacts, geometry_role)
    if geometry_hash(read_xyz(geometry)) != target.geometry_sha256:
        raise _failure("source geometry differs from the scientific target")
    return record, target, wavefunction, geometry


def _conversion_target(
    source_target: JobTarget,
    *,
    source_wavefunction_sha256: str,
    spin_mode: str,
) -> JobTarget:
    return JobTarget(
        stage_type="CONVERT",
        geometry_sha256=source_target.geometry_sha256,
        charge=source_target.charge,
        multiplicity=source_target.multiplicity,
        calculation={
            "adapter": "orca_2mkl",
            "operation": "gbw_to_molden",
            "output_format": "molden",
            "source_target_id": source_target.target_id,
            "source_wavefunction_sha256": source_wavefunction_sha256,
            "spin_mode": spin_mode,
        },
    )


def _result_reuse(
    result_path: Path,
    *,
    target: JobTarget,
    source_wavefunction_sha256: str,
    converter_sha256: str,
) -> tuple[bool, str]:
    if not result_path.is_file():
        return False, "conversion result does not exist"
    try:
        record = read_json(result_path)
        if record.get("target", {}).get("target_id") != target.target_id:
            raise _failure("stored conversion target differs")
        if not record.get("reusable"):
            raise _failure("stored conversion is not reusable")
        provenance = record.get("provenance", {})
        if provenance.get("source_wavefunction_sha256") != source_wavefunction_sha256:
            raise _failure("source wavefunction changed")
        if provenance.get("converter_sha256") != converter_sha256:
            raise _failure("converter executable changed")
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise _failure("conversion artifact manifest is invalid")
        _validated_source_artifact(result_path, artifacts, "wavefunction")
        _validated_source_artifact(result_path, artifacts, "geometry")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return False, str(exc)
    return True, "validated Molden conversion is reusable"


def plan_molden_conversion(
    *,
    source_result: Path,
    output_root: Path,
    converter: Path,
    spin_mode: str,
) -> MoldenConversionPlan:
    """Resolve a deterministic conversion target without executing the converter."""

    normalized_spin = spin_mode.casefold()
    if normalized_spin not in {"restricted", "unrestricted"}:
        raise _failure("spin_mode must be explicitly restricted or unrestricted")
    selected_converter = converter.expanduser().resolve(strict=True)
    if not selected_converter.is_file() or not selected_converter.stat().st_mode & 0o111:
        raise _failure(f"converter is not executable: {selected_converter}")
    _, source_target, wavefunction, geometry = _source_contract(source_result)
    wavefunction_sha = file_hash(wavefunction)
    converter_sha = file_hash(selected_converter)
    target = _conversion_target(
        source_target,
        source_wavefunction_sha256=wavefunction_sha,
        spin_mode=normalized_spin,
    )
    root = output_root.expanduser().resolve()
    layout = ExecutionLayout(
        project_root=root,
        system_identifier=source_target.geometry_sha256,
        workflow_node_identifier="orca_to_molden",
        target_identifier=target.target_id,
        attempt_identifier="attempt_001",
    )
    target_path = layout.target_directory / "target.json"
    result_path = layout.target_directory / "result.json"
    reusable, reason = _result_reuse(
        result_path,
        target=target,
        source_wavefunction_sha256=wavefunction_sha,
        converter_sha256=converter_sha,
    )
    return MoldenConversionPlan(
        source_result=source_result.expanduser().resolve(),
        source_target_id=source_target.target_id,
        source_wavefunction=wavefunction,
        source_wavefunction_sha256=wavefunction_sha,
        source_geometry=geometry,
        geometry_sha256=source_target.geometry_sha256,
        charge=source_target.charge,
        multiplicity=source_target.multiplicity,
        spin_mode=normalized_spin,
        converter=selected_converter,
        converter_sha256=converter_sha,
        output_root=root,
        target=target,
        target_path=target_path,
        result_path=result_path,
        reusable=reusable,
        reusable_reason=reason,
    )


def prepare_molden_conversion(plan: MoldenConversionPlan) -> dict[str, object]:
    """Materialize one isolated conversion attempt and its command contract."""

    if plan.reusable:
        return {**plan.to_dict(), "status": "REUSED"}
    target_directory = plan.target_path.parent
    target_directory.mkdir(parents=True, exist_ok=True)
    target_record = {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "target": plan.target.to_dict(),
        "source_result": str(plan.source_result),
    }
    if plan.target_path.exists():
        if read_json(plan.target_path) != target_record:
            raise _failure("stored conversion target conflicts with its identity")
    else:
        atomic_write_json(plan.target_path, target_record)
    attempt_id = next_attempt_identifier(target_directory)
    layout = ExecutionLayout(
        project_root=plan.output_root,
        system_identifier=plan.geometry_sha256,
        workflow_node_identifier="orca_to_molden",
        target_identifier=plan.target.target_id,
        attempt_identifier=attempt_id,
    )
    layout.create_working_directory()
    atomic_write_json(layout.layout_path, layout.to_dict())
    input_wavefunction = layout.input_path("wavefunction.gbw")
    input_geometry = layout.input_path("geometry.xyz")
    shutil.copy2(plan.source_wavefunction, input_wavefunction)
    shutil.copy2(plan.source_geometry, input_geometry)
    output_wavefunction = layout.output_path("wavefunction.molden.input")
    stdout_path = layout.log_path("orca_2mkl.out")
    stderr_path = layout.log_path("orca_2mkl.err")
    record = {
        **plan.to_dict(),
        "attempt_id": attempt_id,
        "execution_layout": layout.to_dict(),
        "input_wavefunction": str(input_wavefunction),
        "input_geometry": str(input_geometry),
        "output_wavefunction": str(output_wavefunction),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "command": {
            "argv": [str(plan.converter), "wavefunction", "-molden"],
            "working_directory": str(layout.working_directory),
        },
    }
    plan_path = layout.metadata_path("conversion-plan.json")
    atomic_write_json(plan_path, record)
    return {**record, "plan_path": str(plan_path), "status": "PREPARED"}


def _molden_header(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return any(line.strip().casefold() == "[molden format]" for line in handle)


def finalize_molden_conversion(
    *, plan_path: Path, process_exit_code: int
) -> dict[str, object]:
    """Validate one converter result and emit a Multiwfn source-result contract."""

    plan = read_json(plan_path)
    if plan.get("schema_version") != CONVERSION_SCHEMA_VERSION:
        raise _failure("unsupported conversion-plan schema")
    target_value = plan.get("target")
    if not isinstance(target_value, Mapping):
        raise _failure("conversion target is missing")
    target = JobTarget(
        stage_type=str(target_value["stage_type"]),
        geometry_sha256=str(target_value["geometry_sha256"]),
        charge=int(target_value["charge"]),
        multiplicity=int(target_value["multiplicity"]),
        calculation=dict(target_value["calculation"]),
    )
    if target.target_id != target_value.get("target_id"):
        raise _failure("conversion target identity changed")
    source_wavefunction = Path(str(plan["source_wavefunction"]))
    converter = Path(str(plan["converter"]))
    input_geometry = Path(str(plan["input_geometry"]))
    output_wavefunction = Path(str(plan["output_wavefunction"]))
    if file_hash(source_wavefunction) != plan.get("source_wavefunction_sha256"):
        raise _failure("source wavefunction changed during conversion")
    if file_hash(converter) != plan.get("converter_sha256"):
        raise _failure("converter executable changed during conversion")
    if geometry_hash(read_xyz(input_geometry)) != target.geometry_sha256:
        raise _failure("conversion geometry differs from the source target")
    if process_exit_code != 0:
        raise _failure(f"orca_2mkl exited with status {process_exit_code}")
    if not _molden_header(output_wavefunction):
        raise _failure("converter did not produce a non-empty Molden file")
    result_path = Path(str(plan["result_path"]))
    artifacts = {
        "wavefunction": ArtifactRecord.from_path(
            output_wavefunction, role="wavefunction"
        ),
        "geometry": ArtifactRecord.from_path(input_geometry, role="geometry"),
    }
    source_record = read_json(Path(str(plan["source_result"])))
    result = {
        "schema_version": 1,
        "target": target.to_dict(),
        "lineage": {
            "source": "validated_orca_gbw_conversion",
            "geometry_sha256": target.geometry_sha256,
            "parent_stage": source_record["target"]["stage_type"],
            "parent_target_id": source_record["target"]["target_id"],
            "parent_artifact_sha256": target.geometry_sha256,
        },
        "attempt": {
            "attempt_id": plan["attempt_id"],
            "target_id": target.target_id,
        },
        "execution_layout": plan["execution_layout"],
        "execution": {"status": "SUCCESS", "process_exit_code": 0},
        "scientific": {
            "status": "VALID",
            "reason": "GBW was converted to a validated Molden container",
        },
        "wavefunction_semantics": {
            "spin_mode": plan["spin_mode"],
            "format": "molden",
        },
        "artifacts": records_to_dict(artifacts),
        "provenance": {
            "operation": "orca_gbw_to_molden",
            "source_result": plan["source_result"],
            "source_target_id": plan["source_target_id"],
            "source_wavefunction_sha256": plan["source_wavefunction_sha256"],
            "converter": plan["converter"],
            "converter_sha256": plan["converter_sha256"],
            "conversion_plan_sha256": file_hash(plan_path),
        },
        "reusable": True,
    }
    atomic_write_json(result_path, result)
    return result


__all__ = [
    "CONVERSION_SCHEMA_VERSION",
    "MoldenConversionPlan",
    "OrcaWavefunctionConversionError",
    "finalize_molden_conversion",
    "plan_molden_conversion",
    "prepare_molden_conversion",
]
