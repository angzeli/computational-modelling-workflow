"""Fail-closed source-result contract for downstream molecular analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Any, Mapping

from cmw.core.artifacts import (
    ArtifactValidation,
    DensityArtifact,
    ValidationStatus,
    artifact_from_dict,
)
from cmw.core.job import JobTarget
from cmw.core.provenance import file_hash, read_json
from cmw.structure.xyz import geometry_hash, read_xyz


@dataclass(frozen=True)
class FrontierOrbitalIdentity:
    index: int
    source_index: int
    occupation: float
    energy_hartree: float | None = None
    energy_ev: float | None = None


@dataclass(frozen=True)
class FrontierSemantics:
    semantic_contract: str
    spin_mode: str
    indexing: str
    source_indexing: str
    homo: FrontierOrbitalIdentity
    lumo: FrontierOrbitalIdentity


@dataclass(frozen=True)
class WavefunctionSemantics:
    spin_mode: str
    format: str
    homo_index: int | None = None
    lumo_index: int | None = None
    orbital_indexing: str | None = None
    semantic_contract: str | None = None
    frontier_orbitals: FrontierSemantics | None = None


@dataclass(frozen=True)
class ValidatedSource:
    result_path: str
    target_id: str
    geometry_path: str
    geometry_sha256: str
    wavefunction_path: str
    wavefunction_sha256: str
    charge: int
    multiplicity: int
    wavefunction: WavefunctionSemantics
    method: str | None = None
    basis: str | None = None
    upstream_artifact_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["wavefunction"] = asdict(self.wavefunction)
        return value


def _artifact_path(result_path: Path, artifact: Mapping[str, Any]) -> Path:
    value = Path(str(artifact.get("path", "")))
    return value if value.is_absolute() else result_path.parent / value


def _validated_artifact(
    result_path: Path, artifacts: Mapping[str, Any], role: str
) -> tuple[Path, str]:
    artifact = artifacts.get(role)
    if not isinstance(artifact, dict):
        raise ValueError(f"validated source lacks required artifact role: {role}")
    path = _artifact_path(result_path, artifact)
    if not path.is_file():
        raise FileNotFoundError(f"source artifact is missing: {role}: {path}")
    actual_hash = file_hash(path)
    if (
        artifact.get("sha256") != actual_hash
        or artifact.get("size_bytes") != path.stat().st_size
    ):
        raise ValueError(f"source artifact integrity mismatch: {role}")
    return path.resolve(), actual_hash


def validate_source_result(path: Path) -> ValidatedSource:
    """Validate a CMW ORCA result and its explicit wavefunction semantics."""

    result_path = path.resolve(strict=True)
    record = read_json(result_path)
    if record.get("schema_version") != 1:
        raise ValueError("unsupported source-result schema")
    if not record.get("reusable"):
        raise ValueError("source result is not reusable")
    if record.get("execution", {}).get("status") != "SUCCESS":
        raise ValueError("source execution is not successful")
    if record.get("scientific", {}).get("status") != "VALID":
        raise ValueError("source result is not scientifically valid")
    target = record.get("target")
    artifacts = record.get("artifacts")
    semantics = record.get("wavefunction_semantics")
    if not isinstance(target, dict) or not isinstance(artifacts, dict):
        raise ValueError("source target or artifact manifest is invalid")
    parsed_target = JobTarget(
        stage_type=str(target["stage_type"]),
        geometry_sha256=str(target["geometry_sha256"]),
        charge=int(target["charge"]),
        multiplicity=int(target["multiplicity"]),
        calculation=dict(target["calculation"]),
    )
    if parsed_target.target_id != target.get("target_id"):
        raise ValueError("source target identity does not match its content")
    lineage = record.get("lineage")
    if not isinstance(lineage, dict) or not lineage.get("source"):
        raise ValueError("source geometry lineage is missing or ambiguous")
    if lineage.get("geometry_sha256") != parsed_target.geometry_sha256:
        raise ValueError("source lineage geometry differs from its target")
    parent_values = (
        lineage.get("parent_stage"),
        lineage.get("parent_target_id"),
        lineage.get("parent_artifact_sha256"),
    )
    if any(value is not None for value in parent_values) and not all(
        value is not None for value in parent_values
    ):
        raise ValueError("source parent lineage is incomplete")
    if (
        parent_values[2] is not None
        and parent_values[2] != parsed_target.geometry_sha256
    ):
        raise ValueError("source parent artifact does not match the target geometry")
    if not isinstance(semantics, dict):
        raise ValueError("source lacks explicit wavefunction_semantics")
    spin_mode = str(semantics.get("spin_mode", "")).lower()
    if spin_mode not in {"restricted", "unrestricted"}:
        raise ValueError("wavefunction spin_mode must be restricted or unrestricted")
    wavefunction_format = str(semantics.get("format", "")).lower()
    compatible_formats = {"molden", "mwfn", "wfn", "wfx", "fch", "fchk"}
    if wavefunction_format not in compatible_formats:
        raise ValueError(
            "wavefunction_semantics.format must identify a Multiwfn-compatible "
            f"artifact ({', '.join(sorted(compatible_formats))}); GBW requires explicit conversion"
        )
    homo = semantics.get("homo_index")
    lumo = semantics.get("lumo_index")
    if homo is not None and (not isinstance(homo, int) or homo < 1):
        raise ValueError("HOMO index must be a positive one-based integer")
    if lumo is not None and (not isinstance(lumo, int) or lumo < 1):
        raise ValueError("LUMO index must be a positive one-based integer")
    indexing_value = semantics.get("orbital_indexing")
    if indexing_value is None and (homo is not None or lumo is not None):
        orbital_indexing = "one_based"
    elif indexing_value is None:
        orbital_indexing = None
    else:
        orbital_indexing = str(indexing_value).casefold()
        if orbital_indexing != "one_based":
            raise ValueError("frontier orbital indexing must be explicitly one_based")
    semantic_contract = (
        str(semantics["semantic_contract"])
        if semantics.get("semantic_contract") is not None
        else None
    )
    frontier_semantics = None
    raw_frontier = semantics.get("frontier_orbitals")
    if isinstance(raw_frontier, Mapping) and raw_frontier.get("status") is None:
        if spin_mode != "restricted":
            raise ValueError("unrestricted frontier semantics require explicit spin channels")
        try:
            raw_homo = raw_frontier["homo"]
            raw_lumo = raw_frontier["lumo"]
            if not isinstance(raw_homo, Mapping) or not isinstance(raw_lumo, Mapping):
                raise TypeError("frontier orbital records must be mappings")
            frontier_semantics = FrontierSemantics(
                semantic_contract=str(raw_frontier["semantic_contract"]),
                spin_mode=str(raw_frontier["spin_mode"]).casefold(),
                indexing=str(raw_frontier["indexing"]).casefold(),
                source_indexing=str(raw_frontier["source_indexing"]),
                homo=FrontierOrbitalIdentity(
                    index=int(raw_homo["index"]),
                    source_index=int(raw_homo["source_index"]),
                    occupation=float(raw_homo["occupation"]),
                    energy_hartree=(
                        float(raw_homo["energy_hartree"])
                        if raw_homo.get("energy_hartree") is not None
                        else None
                    ),
                    energy_ev=(
                        float(raw_homo["energy_ev"])
                        if raw_homo.get("energy_ev") is not None
                        else None
                    ),
                ),
                lumo=FrontierOrbitalIdentity(
                    index=int(raw_lumo["index"]),
                    source_index=int(raw_lumo["source_index"]),
                    occupation=float(raw_lumo["occupation"]),
                    energy_hartree=(
                        float(raw_lumo["energy_hartree"])
                        if raw_lumo.get("energy_hartree") is not None
                        else None
                    ),
                    energy_ev=(
                        float(raw_lumo["energy_ev"])
                        if raw_lumo.get("energy_ev") is not None
                        else None
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("frontier_orbitals semantics are incomplete") from exc
        if (
            frontier_semantics.semantic_contract != semantic_contract
            or frontier_semantics.spin_mode != "restricted"
            or frontier_semantics.indexing != "one_based"
            or frontier_semantics.homo.index != homo
            or frontier_semantics.lumo.index != lumo
            or frontier_semantics.lumo.index != frontier_semantics.homo.index + 1
        ):
            raise ValueError("nested frontier semantics contradict flat one-based indices")
        for label, orbital, expected_occupation in (
            ("HOMO", frontier_semantics.homo, 2.0),
            ("LUMO", frontier_semantics.lumo, 0.0),
        ):
            if orbital.index < 1 or orbital.source_index < 0:
                raise ValueError(f"{label} frontier identity is invalid")
            values = (
                orbital.occupation,
                *(
                    value
                    for value in (orbital.energy_hartree, orbital.energy_ev)
                    if value is not None
                ),
            )
            if any(not math.isfinite(value) for value in values):
                raise ValueError(f"{label} frontier metadata is not finite")
            if abs(orbital.occupation - expected_occupation) > 1.0e-6:
                raise ValueError(f"{label} frontier occupation is incompatible")
    elif semantic_contract is not None and spin_mode == "restricted":
        raise ValueError("restricted semantic contract lacks frontier_orbitals evidence")
    wavefunction_path, wavefunction_hash = _validated_artifact(
        result_path, artifacts, "wavefunction"
    )
    geometry_path, geometry_artifact_hash = _validated_artifact(
        result_path, artifacts, "geometry"
    )
    geometry = read_xyz(geometry_path)
    identity = geometry_hash(geometry)
    if target.get("geometry_sha256") != identity:
        raise ValueError("source geometry lineage does not match the target")
    if geometry_artifact_hash != artifacts["geometry"].get("sha256"):
        raise ValueError("source geometry artifact identity is ambiguous")
    target_id = parsed_target.target_id
    method = None
    basis = None
    upstream_artifact_id = None
    typed = record.get("scientific_artifact")
    if typed is not None:
        if not isinstance(typed, Mapping):
            raise ValueError("source scientific_artifact must be a mapping")
        parsed_artifact = artifact_from_dict(typed)
        if (
            parsed_artifact.producing_calculation != target_id
            or not parsed_artifact.validation.passed
        ):
            raise ValueError("source scientific_artifact is invalid or mismatched")
        method = parsed_artifact.method
        basis = parsed_artifact.basis
        upstream_artifact_id = parsed_artifact.artifact_id
    return ValidatedSource(
        result_path=str(result_path),
        target_id=target_id,
        geometry_path=str(geometry_path),
        geometry_sha256=identity,
        wavefunction_path=str(wavefunction_path),
        wavefunction_sha256=wavefunction_hash,
        charge=int(target["charge"]),
        multiplicity=int(target["multiplicity"]),
        wavefunction=WavefunctionSemantics(
            spin_mode,
            wavefunction_format,
            homo,
            lumo,
            orbital_indexing,
            semantic_contract,
            frontier_semantics,
        ),
        method=method,
        basis=basis,
        upstream_artifact_id=upstream_artifact_id,
    )


def density_artifact_from_source(source: ValidatedSource) -> DensityArtifact:
    """Expose a validated wavefunction as an explicit density-source artifact."""

    parents = (
        (source.upstream_artifact_id,)
        if source.upstream_artifact_id is not None
        else ()
    )
    return DensityArtifact(
        producing_calculation=source.target_id,
        method=source.method,
        basis=source.basis,
        protocol={
            "source_format": source.wavefunction.format,
            "spin_mode": source.wavefunction.spin_mode,
        },
        parent_artifacts=parents,
        files={
            "wavefunction": source.wavefunction_path,
            "geometry": source.geometry_path,
        },
        validation=ArtifactValidation(
            ValidationStatus.PASSED,
            {
                "source_reusable": True,
                "wavefunction_integrity": True,
                "geometry_identity": True,
            },
            "VALID_DENSITY_SOURCE",
            "validated wavefunction and geometry provide an IGMH density source",
        ),
        provenance={
            "source_result": source.result_path,
            "wavefunction_sha256": source.wavefunction_sha256,
            "geometry_sha256": source.geometry_sha256,
        },
        metadata={"density_source": "validated_wavefunction"},
    )


__all__ = [
    "ValidatedSource",
    "FrontierOrbitalIdentity",
    "FrontierSemantics",
    "WavefunctionSemantics",
    "density_artifact_from_source",
    "validate_source_result",
]
