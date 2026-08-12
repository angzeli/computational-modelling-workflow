"""Explicit fragment and grid semantics for interfragment IGMH cubes."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path

from cmw.core.provenance import read_json


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

    def __post_init__(self) -> None:
        if not math.isfinite(self.grid_spacing_bohr) or self.grid_spacing_bohr <= 0:
            raise ValueError("IGMH grid spacing must be finite and positive")
        if self.profile != "interfragment":
            raise ValueError("only the interfragment IGMH profile is supported")


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
            or any(not isinstance(value, int) or isinstance(value, bool) for value in values)
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
        raise ValueError(f"fragment definitions are an incomplete partition; missing {missing}")
    return FragmentDefinition(parsed["A"], parsed["B"])


def load_igmh_configuration(path: Path) -> IgmhConfiguration:
    record = read_json(path)
    if record.get("schema_version") != 1:
        raise ValueError("unsupported IGMH configuration schema")
    if "grid_spacing_bohr" not in record:
        raise ValueError("IGMH grid_spacing_bohr is required; no project default is assumed")
    return IgmhConfiguration(
        float(record["grid_spacing_bohr"]), str(record.get("profile", "interfragment"))
    )
