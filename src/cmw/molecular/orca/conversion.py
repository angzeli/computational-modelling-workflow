"""Validated ORCA wavefunction conversion for downstream analysis."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import shutil
from typing import Any, Mapping

from cmw.core.artifacts import (
    ArtifactValidation,
    ValidationStatus,
    WavefunctionArtifact,
)
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

from .properties import (
    FrontierOrbitalSemantics,
    parse_ground_state_frontier_orbitals,
)


CONVERSION_SCHEMA_VERSION = 1
FRONTIER_SEMANTIC_CONTRACT = "orca_molden_frontier_v1"


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
    source_result_sha256: str
    source_target_id: str
    source_attempt_id: str | None
    source_scientific_artifact_id: str | None
    source_output: Path
    source_output_sha256: str
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
            "source_result_sha256": self.source_result_sha256,
            "source_target_id": self.source_target_id,
            "source_attempt_id": self.source_attempt_id,
            "source_scientific_artifact_id": self.source_scientific_artifact_id,
            "source_output": str(self.source_output),
            "source_output_sha256": self.source_output_sha256,
            "source_wavefunction": str(self.source_wavefunction),
            "source_wavefunction_sha256": self.source_wavefunction_sha256,
            "source_geometry": str(self.source_geometry),
            "geometry_sha256": self.geometry_sha256,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "spin_mode": self.spin_mode,
            "semantic_contract": FRONTIER_SEMANTIC_CONTRACT,
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
) -> tuple[dict[str, Any], JobTarget, Path, Path, Path]:
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
    output = _validated_source_artifact(result_path, artifacts, "output")
    return record, target, wavefunction, geometry, output


def _conversion_target(
    source_target: JobTarget,
    *,
    source_wavefunction_sha256: str,
    source_output_sha256: str,
    source_result_sha256: str,
    source_attempt_id: str | None,
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
            "source_attempt_id": source_attempt_id,
            "source_result_sha256": source_result_sha256,
            "source_output_sha256": source_output_sha256,
            "source_wavefunction_sha256": source_wavefunction_sha256,
            "spin_mode": spin_mode,
            "semantic_contract": FRONTIER_SEMANTIC_CONTRACT,
        },
    )


def _result_reuse(
    result_path: Path,
    *,
    target: JobTarget,
    source_result_sha256: str,
    source_output_sha256: str,
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
        if provenance.get("source_result_sha256") != source_result_sha256:
            raise _failure("source result changed")
        if provenance.get("source_output_sha256") != source_output_sha256:
            raise _failure("source ORCA output changed")
        if provenance.get("source_wavefunction_sha256") != source_wavefunction_sha256:
            raise _failure("source wavefunction changed")
        if provenance.get("converter_sha256") != converter_sha256:
            raise _failure("converter executable changed")
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise _failure("conversion artifact manifest is invalid")
        _validated_source_artifact(result_path, artifacts, "wavefunction")
        _validated_source_artifact(result_path, artifacts, "geometry")
        semantics = record.get("wavefunction_semantics")
        if (
            not isinstance(semantics, Mapping)
            or semantics.get("semantic_contract") != FRONTIER_SEMANTIC_CONTRACT
        ):
            raise _failure("stored conversion lacks validated frontier semantics")
        from cmw.molecular.workflows.source import validate_source_result

        validate_source_result(result_path)
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
    source_record, source_target, wavefunction, geometry, output = _source_contract(
        source_result
    )
    source_result_path = source_result.expanduser().resolve()
    source_result_sha = file_hash(source_result_path)
    wavefunction_sha = file_hash(wavefunction)
    output_sha = file_hash(output)
    converter_sha = file_hash(selected_converter)
    raw_attempt = source_record.get("attempt")
    source_attempt_id = (
        str(raw_attempt.get("attempt_id"))
        if isinstance(raw_attempt, Mapping) and raw_attempt.get("attempt_id")
        else None
    )
    raw_scientific_artifact = source_record.get("scientific_artifact")
    source_scientific_artifact_id = (
        str(raw_scientific_artifact.get("artifact_id"))
        if isinstance(raw_scientific_artifact, Mapping)
        and raw_scientific_artifact.get("artifact_id")
        else None
    )
    target = _conversion_target(
        source_target,
        source_wavefunction_sha256=wavefunction_sha,
        source_output_sha256=output_sha,
        source_result_sha256=source_result_sha,
        source_attempt_id=source_attempt_id,
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
        source_result_sha256=source_result_sha,
        source_output_sha256=output_sha,
        source_wavefunction_sha256=wavefunction_sha,
        converter_sha256=converter_sha,
    )
    return MoldenConversionPlan(
        source_result=source_result_path,
        source_result_sha256=source_result_sha,
        source_target_id=source_target.target_id,
        source_attempt_id=source_attempt_id,
        source_scientific_artifact_id=source_scientific_artifact_id,
        source_output=output,
        source_output_sha256=output_sha,
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
    input_output = layout.input_path("source.out")
    shutil.copy2(plan.source_wavefunction, input_wavefunction)
    shutil.copy2(plan.source_geometry, input_geometry)
    shutil.copy2(plan.source_output, input_output)
    output_wavefunction = layout.output_path("wavefunction.molden.input")
    stdout_path = layout.log_path("orca_2mkl.out")
    stderr_path = layout.log_path("orca_2mkl.err")
    record = {
        **plan.to_dict(),
        "attempt_id": attempt_id,
        "execution_layout": layout.to_dict(),
        "input_wavefunction": str(input_wavefunction),
        "input_geometry": str(input_geometry),
        "input_output": str(input_output),
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


def _finite_number(value: str, *, field: str) -> float:
    try:
        parsed = float(value.replace("D", "E").replace("d", "e"))
    except ValueError as exc:
        raise _failure(f"Molden {field} is not numeric") from exc
    if not math.isfinite(parsed):
        raise _failure(f"Molden {field} is not finite")
    return parsed


def _molden_orbitals(path: Path) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    current: dict[str, object] = {}
    in_mo = False
    field = re.compile(r"^\s*(Sym|Ene|Spin|Occup)\s*=\s*(.*?)\s*$", re.I)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.casefold() == "[mo]":
            in_mo = True
            continue
        if in_mo and stripped.startswith("["):
            break
        if not in_mo:
            continue
        match = field.match(line)
        if match is None:
            continue
        key, raw = match.groups()
        normalized = key.casefold()
        if normalized == "sym" and current:
            records.append(current)
            current = {}
        if normalized in {"ene", "occup"}:
            current[normalized] = _finite_number(raw, field=normalized)
        else:
            current[normalized] = raw.strip()
    if current:
        records.append(current)
    required = {"sym", "ene", "spin", "occup"}
    if not records or any(not required.issubset(record) for record in records):
        raise _failure("Molden [MO] records are missing or incomplete")
    return tuple(records)


def _cross_validate_molden_frontier(
    path: Path, frontier: FrontierOrbitalSemantics
) -> dict[str, object]:
    records = _molden_orbitals(path)
    if any(str(record["spin"]).casefold() != "alpha" for record in records):
        raise _failure("restricted Molden output contains inconsistent spin channels")
    occupied = []
    virtual = []
    virtual_started = False
    for ordinal, record in enumerate(records, start=1):
        occupation = float(record["occup"])
        if abs(occupation - 2.0) <= 1.0e-6:
            if virtual_started:
                raise _failure("Molden orbital order becomes occupied after a virtual orbital")
            occupied.append((ordinal, record))
        elif abs(occupation) <= 1.0e-6:
            virtual_started = True
            virtual.append((ordinal, record))
        else:
            raise _failure("Molden restricted frontier contains a partial occupation")
    if not occupied or not virtual:
        raise _failure("Molden occupations do not identify HOMO and LUMO")
    homo_ordinal, homo = occupied[-1]
    lumo_ordinal, lumo = virtual[0]
    if (homo_ordinal, lumo_ordinal) != (
        frontier.homo.index,
        frontier.lumo.index,
    ):
        raise _failure("ORCA and Molden HOMO/LUMO one-based ordinals disagree")
    for label, source, converted in (
        ("HOMO", frontier.homo, homo),
        ("LUMO", frontier.lumo, lumo),
    ):
        if abs(source.occupation - float(converted["occup"])) > 1.0e-6:
            raise _failure(f"ORCA and Molden {label} occupations disagree")
        if abs(source.energy_hartree - float(converted["ene"])) > 1.0e-6:
            raise _failure(f"ORCA and Molden {label} energies disagree")
    return {
        "status": "PASSED",
        "molden_orbital_count": len(records),
        "homo_ordinal_match": True,
        "lumo_ordinal_match": True,
        "frontier_occupations_match": True,
        "frontier_energies_match": True,
    }


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
    source_result = Path(str(plan["source_result"]))
    source_output = Path(str(plan["source_output"]))
    input_output = Path(str(plan["input_output"]))
    converter = Path(str(plan["converter"]))
    input_geometry = Path(str(plan["input_geometry"]))
    output_wavefunction = Path(str(plan["output_wavefunction"]))
    if file_hash(source_result) != plan.get("source_result_sha256"):
        raise _failure("source result changed during conversion")
    if file_hash(source_output) != plan.get("source_output_sha256"):
        raise _failure("source ORCA output changed during conversion")
    if file_hash(input_output) != plan.get("source_output_sha256"):
        raise _failure("attempt-local ORCA output differs from the source result")
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
    if plan["spin_mode"] == "restricted":
        frontier = parse_ground_state_frontier_orbitals(
            input_output.read_text(encoding="utf-8", errors="replace"),
            spin_mode="restricted",
        )
        molden_validation = _cross_validate_molden_frontier(
            output_wavefunction, frontier
        )
        wavefunction_semantics = {
            "semantic_contract": FRONTIER_SEMANTIC_CONTRACT,
            "spin_mode": "restricted",
            "format": "molden",
            "orbital_indexing": "one_based",
            "homo_index": frontier.homo.index,
            "lumo_index": frontier.lumo.index,
            "frontier_orbitals": frontier.to_dict(),
            "molden_cross_validation": molden_validation,
        }
    else:
        wavefunction_semantics = {
            "semantic_contract": FRONTIER_SEMANTIC_CONTRACT,
            "spin_mode": "unrestricted",
            "format": "molden",
            "orbital_indexing": None,
            "homo_index": None,
            "lumo_index": None,
            "frontier_orbitals": {
                "status": "UNSUPPORTED_SPIN_MODE",
                "reason": "alpha/beta frontier channels are not collapsed",
            },
            "molden_cross_validation": {"status": "NOT_APPLICABLE"},
        }
    result_path = Path(str(plan["result_path"]))
    artifacts = {
        "wavefunction": ArtifactRecord.from_path(
            output_wavefunction, role="wavefunction"
        ),
        "geometry": ArtifactRecord.from_path(input_geometry, role="geometry"),
    }
    source_record = read_json(source_result)
    source_artifact = source_record.get("scientific_artifact")
    parent_artifacts = (
        (str(source_artifact["artifact_id"]),)
        if isinstance(source_artifact, Mapping) and source_artifact.get("artifact_id")
        else ()
    )
    method = (
        str(source_artifact["method"])
        if isinstance(source_artifact, Mapping)
        and source_artifact.get("method") is not None
        else None
    )
    basis = (
        str(source_artifact["basis"])
        if isinstance(source_artifact, Mapping)
        and source_artifact.get("basis") is not None
        else None
    )
    wavefunction_artifact = WavefunctionArtifact(
        producing_calculation=target.target_id,
        method=method,
        basis=basis,
        protocol={
            "operation": "orca_gbw_to_molden",
            "semantic_contract": FRONTIER_SEMANTIC_CONTRACT,
        },
        parent_artifacts=parent_artifacts,
        files={
            "wavefunction": str(output_wavefunction),
            "geometry": str(input_geometry),
        },
        validation=ArtifactValidation(
            ValidationStatus.PASSED,
            {
                "source_result_valid": True,
                "source_orca_output_integrity": True,
                "source_gbw_integrity": True,
                "molden_container_valid": True,
                "frontier_semantics_valid": (
                    True if plan["spin_mode"] == "restricted" else None
                ),
                "molden_frontier_cross_validation": (
                    True if plan["spin_mode"] == "restricted" else None
                ),
            },
            "VALID_MOLDEN_WAVEFUNCTION",
            "validated ORCA conversion preserves explicit wavefunction semantics",
        ),
        provenance={
            "source_result": str(source_result),
            "source_result_sha256": plan["source_result_sha256"],
            "source_target_id": plan["source_target_id"],
            "source_attempt_id": plan.get("source_attempt_id"),
            "source_output_sha256": plan["source_output_sha256"],
            "source_wavefunction_sha256": plan["source_wavefunction_sha256"],
            "geometry_sha256": target.geometry_sha256,
            "converter_sha256": plan["converter_sha256"],
        },
        metadata={"wavefunction_semantics": wavefunction_semantics},
    )
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
        "wavefunction_semantics": wavefunction_semantics,
        "scientific_artifact": wavefunction_artifact.to_dict(),
        "artifacts": records_to_dict(artifacts),
        "provenance": {
            "operation": "orca_gbw_to_molden",
            "source_result": plan["source_result"],
            "source_result_sha256": plan["source_result_sha256"],
            "source_target_id": plan["source_target_id"],
            "source_attempt_id": plan.get("source_attempt_id"),
            "source_scientific_artifact_id": plan.get(
                "source_scientific_artifact_id"
            ),
            "source_output": plan["source_output"],
            "source_output_sha256": plan["source_output_sha256"],
            "source_wavefunction_sha256": plan["source_wavefunction_sha256"],
            "converter": plan["converter"],
            "converter_sha256": plan["converter_sha256"],
            "conversion_plan_sha256": file_hash(plan_path),
            "conversion_target_id": target.target_id,
            "semantic_contract": FRONTIER_SEMANTIC_CONTRACT,
        },
        "reusable": True,
    }
    atomic_write_json(result_path, result)
    return result


__all__ = [
    "CONVERSION_SCHEMA_VERSION",
    "FRONTIER_SEMANTIC_CONTRACT",
    "MoldenConversionPlan",
    "OrcaWavefunctionConversionError",
    "finalize_molden_conversion",
    "plan_molden_conversion",
    "prepare_molden_conversion",
]
