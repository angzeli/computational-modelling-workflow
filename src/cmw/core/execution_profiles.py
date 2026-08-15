"""Generic runtime resource policies kept separate from scientific intent."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Mapping

from .provenance import stable_hash


EXECUTION_PROFILE_SCHEMA_VERSION = 1


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _positive_integer(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_memory(value: object, *, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _resource_keys(value: Mapping[str, Any], expected: set[str], *, name: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unsupported " + ", ".join(extra))
        raise ValueError(f"{name} resource definition is malformed: {'; '.join(detail)}")


@dataclass(frozen=True)
class OrcaResourcePolicy:
    nprocs: int
    total_memory_gb: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "nprocs", _positive_integer(self.nprocs, name="orca.nprocs")
        )
        object.__setattr__(
            self,
            "total_memory_gb",
            _positive_memory(
                self.total_memory_gb, name="orca.total_memory_gb"
            ),
        )

    def to_dict(self) -> dict[str, int | float]:
        return {
            "nprocs": self.nprocs,
            "total_memory_gb": self.total_memory_gb,
        }


@dataclass(frozen=True)
class MultiwfnResourcePolicy:
    nthreads: int
    total_memory_gb: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nthreads",
            _positive_integer(self.nthreads, name="multiwfn.nthreads"),
        )
        object.__setattr__(
            self,
            "total_memory_gb",
            _positive_memory(
                self.total_memory_gb, name="multiwfn.total_memory_gb"
            ),
        )

    def to_dict(self) -> dict[str, int | float]:
        return {
            "nthreads": self.nthreads,
            "total_memory_gb": self.total_memory_gb,
        }


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    orca: OrcaResourcePolicy
    multiwfn: MultiwfnResourcePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("execution profile name is required")

    @property
    def execution_profile_hash(self) -> str:
        return stable_hash(
            {
                "schema_version": EXECUTION_PROFILE_SCHEMA_VERSION,
                "name": self.name,
                "orca": self.orca.to_dict(),
                "multiwfn": self.multiwfn.to_dict(),
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "execution_profile_hash": self.execution_profile_hash,
            "orca": self.orca.to_dict(),
            "multiwfn": self.multiwfn.to_dict(),
        }


@dataclass(frozen=True)
class ExecutionProfiles:
    active_profile: str
    profiles: Mapping[str, ExecutionProfile]

    def __post_init__(self) -> None:
        profiles = dict(self.profiles)
        if not isinstance(self.active_profile, str) or not self.active_profile.strip():
            raise ValueError("execution configuration requires active_profile")
        if not profiles:
            raise ValueError("execution configuration requires at least one profile")
        if self.active_profile not in profiles:
            raise ValueError(
                f"unknown active execution profile: {self.active_profile!r}"
            )
        object.__setattr__(self, "profiles", profiles)

    @property
    def selected(self) -> ExecutionProfile:
        return self.profiles[self.active_profile]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EXECUTION_PROFILE_SCHEMA_VERSION,
            "active_profile": self.active_profile,
            "profiles": {
                name: profile.to_dict() for name, profile in self.profiles.items()
            },
        }


def execution_profiles_from_mapping(value: Mapping[str, Any]) -> ExecutionProfiles:
    _resource_keys(
        value,
        {"schema_version", "active_profile", "profiles"},
        name="execution configuration",
    )
    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version != EXECUTION_PROFILE_SCHEMA_VERSION
    ):
        raise ValueError("unsupported execution-profile schema")
    active_profile = value["active_profile"]
    if not isinstance(active_profile, str) or not active_profile.strip():
        raise ValueError("execution configuration requires active_profile")
    raw_profiles = _mapping(value.get("profiles"), name="profiles")
    profiles: dict[str, ExecutionProfile] = {}
    for raw_name, raw_profile in raw_profiles.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("execution profile names must be non-empty strings")
        profile = _mapping(raw_profile, name=f"profiles.{raw_name}")
        _resource_keys(profile, {"orca", "multiwfn"}, name=f"profiles.{raw_name}")
        orca = _mapping(profile["orca"], name=f"profiles.{raw_name}.orca")
        multiwfn = _mapping(
            profile["multiwfn"], name=f"profiles.{raw_name}.multiwfn"
        )
        _resource_keys(
            orca,
            {"nprocs", "total_memory_gb"},
            name=f"profiles.{raw_name}.orca",
        )
        _resource_keys(
            multiwfn,
            {"nthreads", "total_memory_gb"},
            name=f"profiles.{raw_name}.multiwfn",
        )
        profiles[raw_name] = ExecutionProfile(
            raw_name,
            OrcaResourcePolicy(orca["nprocs"], orca["total_memory_gb"]),
            MultiwfnResourcePolicy(
                multiwfn["nthreads"], multiwfn["total_memory_gb"]
            ),
        )
    return ExecutionProfiles(active_profile, profiles)


def load_execution_profiles(path: Path) -> ExecutionProfiles:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise RuntimeError("execution-profile YAML support requires PyYAML") from exc
    resolved = path.expanduser().resolve()
    try:
        value = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load execution configuration: {exc}") from exc
    return execution_profiles_from_mapping(
        _mapping(value, name="execution configuration")
    )


__all__ = [
    "EXECUTION_PROFILE_SCHEMA_VERSION",
    "ExecutionProfile",
    "ExecutionProfiles",
    "MultiwfnResourcePolicy",
    "OrcaResourcePolicy",
    "execution_profiles_from_mapping",
    "load_execution_profiles",
]
