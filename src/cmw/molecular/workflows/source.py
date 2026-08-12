"""Fail-closed source-result contract for downstream molecular analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from cmw.core.provenance import file_hash, read_json
from cmw.structure.xyz import geometry_hash, read_xyz


@dataclass(frozen=True)
class WavefunctionSemantics:
    spin_mode: str
    homo_index: int | None = None
    lumo_index: int | None = None


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
    if artifact.get("sha256") != actual_hash or artifact.get("size_bytes") != path.stat().st_size:
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
    if not isinstance(semantics, dict):
        raise ValueError("source lacks explicit wavefunction_semantics")
    spin_mode = str(semantics.get("spin_mode", "")).lower()
    if spin_mode not in {"restricted", "unrestricted"}:
        raise ValueError("wavefunction spin_mode must be restricted or unrestricted")
    homo = semantics.get("homo_index")
    lumo = semantics.get("lumo_index")
    if homo is not None and (not isinstance(homo, int) or homo < 1):
        raise ValueError("HOMO index must be a positive one-based integer")
    if lumo is not None and (not isinstance(lumo, int) or lumo < 1):
        raise ValueError("LUMO index must be a positive one-based integer")
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
    target_id = str(target.get("target_id", ""))
    if not target_id:
        raise ValueError("source target identity is missing")
    return ValidatedSource(
        result_path=str(result_path),
        target_id=target_id,
        geometry_path=str(geometry_path),
        geometry_sha256=identity,
        wavefunction_path=str(wavefunction_path),
        wavefunction_sha256=wavefunction_hash,
        charge=int(target["charge"]),
        multiplicity=int(target["multiplicity"]),
        wavefunction=WavefunctionSemantics(spin_mode, homo, lumo),
    )
