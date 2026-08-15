"""Explicit fragment and grid semantics for interfragment IGMH cubes."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.provenance import read_json
from cmw.molecular.multiwfn.adapter import MultiwfnOutputSpec, validate_output_specs


REQUIRED_INTERFRAGMENT_ROLES = frozenset(
    {"delta_g_inter_cube", "sign_lambda2_rho_cube"}
)


def default_igmh_outputs() -> tuple[MultiwfnOutputSpec, ...]:
    """Backward-compatible public names for the supported 3.8 profile."""

    return (
        MultiwfnOutputSpec(
            "delta_g_inter_cube",
            "dg_inter.cub",
            "dg_inter.cub",
            visualization={"field": "delta_g_inter"},
        ),
        MultiwfnOutputSpec(
            "sign_lambda2_rho_cube",
            "sl2r.cub",
            "sl2r.cub",
            visualization={"field": "sign_lambda2_rho"},
        ),
    )


@dataclass(frozen=True)
class FragmentDefinition:
    fragment_a: tuple[int, ...]
    fragment_b: tuple[int, ...]
    indexing: str = "one_based"
    require_complete_partition: bool = True
    allow_overlap: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IgmhConfiguration:
    grid_spacing_bohr: float
    profile: str = "interfragment"
    cube_generation: bool = True
    outputs: tuple[MultiwfnOutputSpec, ...] = field(
        default_factory=default_igmh_outputs
    )
    visualization: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(self.grid_spacing_bohr) or self.grid_spacing_bohr <= 0:
            raise ValueError("IGMH grid spacing must be finite and positive")
        if self.profile != "interfragment":
            raise ValueError("only the interfragment IGMH profile is supported")
        if not isinstance(self.cube_generation, bool):
            raise ValueError("IGMH cube_generation must be a boolean")
        if not self.cube_generation:
            raise ValueError(
                "the reusable IGMH artifact workflow requires cube generation"
            )
        outputs = validate_output_specs(self.outputs)
        roles = {item.role for item in outputs if item.required}
        missing = sorted(REQUIRED_INTERFRAGMENT_ROLES - roles)
        if missing:
            raise ValueError(
                "interfragment IGMH outputs are missing required roles: "
                + ", ".join(missing)
            )
        incompatible = sorted(
            item.role
            for item in outputs
            if item.role in REQUIRED_INTERFRAGMENT_ROLES
            and item.media_type != "application/x-gaussian-cube"
        )
        if incompatible:
            raise ValueError(
                "interfragment IGMH fields must use cube media types: "
                + ", ".join(incompatible)
            )
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "visualization", dict(self.visualization))

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "grid_spacing_bohr": self.grid_spacing_bohr,
            "cube_generation": self.cube_generation,
            "outputs": [item.to_dict() for item in self.outputs],
        }


def _outputs(value: object) -> tuple[MultiwfnOutputSpec, ...]:
    if value is None:
        return default_igmh_outputs()
    items: list[MultiwfnOutputSpec] = []
    if isinstance(value, Mapping):
        for role, raw in value.items():
            if isinstance(raw, str):
                items.append(MultiwfnOutputSpec(str(role), raw, raw))
            elif isinstance(raw, Mapping):
                items.append(
                    MultiwfnOutputSpec.from_mapping({"role": str(role), **dict(raw)})
                )
            else:
                raise ValueError("IGMH output mappings require strings or objects")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("IGMH outputs must contain objects")
            items.append(MultiwfnOutputSpec.from_mapping(item))
    else:
        raise ValueError("IGMH outputs must be a mapping or list")
    return tuple(items)


def load_fragments(path: Path, atom_count: int) -> FragmentDefinition:
    record = read_json(path)
    if record.get("schema_version") != 1:
        raise ValueError("unsupported fragment schema")
    if record.get("indexing") != "one_based":
        raise ValueError("fragment indexing must be explicitly one_based")
    if record.get("allow_overlap") is not False:
        raise ValueError("fragment overlap must be explicitly prohibited")
    if record.get("require_complete_partition") is not True:
        raise ValueError("fragments must explicitly require a complete partition")
    fragments = record.get("fragments")
    if not isinstance(fragments, dict) or set(fragments) != {"A", "B"}:
        raise ValueError("fragment configuration must define exactly A and B")
    parsed: dict[str, tuple[int, ...]] = {}
    for name in ("A", "B"):
        values = fragments[name]
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in values
            )
        ):
            raise ValueError(f"fragment {name} must contain one-based integer indices")
        if len(set(values)) != len(values):
            raise ValueError(f"fragment {name} contains duplicate atom indices")
        invalid = [value for value in values if value < 1 or value > atom_count]
        if invalid:
            raise ValueError(f"fragment {name} has invalid atom index: {invalid[0]}")
        parsed[name] = tuple(sorted(values))
    overlap = set(parsed["A"]) & set(parsed["B"])
    if overlap:
        raise ValueError(f"fragment definitions overlap at atom index {min(overlap)}")
    expected = set(range(1, atom_count + 1))
    supplied = set(parsed["A"]) | set(parsed["B"])
    if supplied != expected:
        missing = sorted(expected - supplied)
        raise ValueError(
            f"fragment definitions are an incomplete partition; missing {missing}"
        )
    return FragmentDefinition(parsed["A"], parsed["B"])


def load_igmh_configuration(path: Path) -> IgmhConfiguration:
    record = read_json(path)
    if record.get("schema_version") != 1:
        raise ValueError("unsupported IGMH configuration schema")
    if "grid_spacing_bohr" not in record:
        raise ValueError(
            "IGMH grid_spacing_bohr is required; no project default is assumed"
        )
    cube_generation = record.get("cube_generation", True)
    if not isinstance(cube_generation, bool):
        raise ValueError("IGMH cube_generation must be a boolean")
    visualization = record.get("visualization", {})
    if not isinstance(visualization, Mapping):
        raise ValueError("IGMH visualization metadata must be a mapping")
    return IgmhConfiguration(
        float(record["grid_spacing_bohr"]),
        str(record.get("profile", record.get("mode", "interfragment"))),
        cube_generation,
        _outputs(record.get("outputs")),
        dict(visualization),
    )


__all__ = [
    "FragmentDefinition",
    "IgmhConfiguration",
    "REQUIRED_INTERFRAGMENT_ROLES",
    "default_igmh_outputs",
    "load_fragments",
    "load_igmh_configuration",
]
