"""Typed HOF configuration after external-schema normalization."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

from cmw.core.provenance import stable_hash
from cmw.structure.xyz import XYZGeometry, geometry_hash


HOF_ADAPTER_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class HofFragment:
    """One explicit molecular fragment with zero-based internal atom indices."""

    fragment_id: str
    atom_indices: tuple[int, ...]
    charge: int
    multiplicity: int

    def __post_init__(self) -> None:
        if not self.fragment_id:
            raise ValueError("fragment id is required")
        if not self.atom_indices:
            raise ValueError(f"fragment {self.fragment_id!r} must contain atoms")
        if len(set(self.atom_indices)) != len(self.atom_indices):
            raise ValueError(f"fragment {self.fragment_id!r} contains duplicate atoms")
        if self.multiplicity < 1:
            raise ValueError(
                f"fragment {self.fragment_id!r} multiplicity must be positive"
            )
        object.__setattr__(self, "atom_indices", tuple(sorted(self.atom_indices)))

    def to_dict(self, *, atom_index_base: int = 0) -> dict[str, object]:
        return {
            "fragment_id": self.fragment_id,
            "atom_indices": [index + atom_index_base for index in self.atom_indices],
            "charge": self.charge,
            "multiplicity": self.multiplicity,
        }


@dataclass(frozen=True)
class HofHydrogenBond:
    """One donor-hydrogen-acceptor definition using zero-based atom indices."""

    bond_id: str
    donor_atom: int
    hydrogen_atom: int
    acceptor_atom: int
    symmetry_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.bond_id:
            raise ValueError("hydrogen-bond id is required")
        if len({self.donor_atom, self.hydrogen_atom, self.acceptor_atom}) != 3:
            raise ValueError(
                f"hydrogen bond {self.bond_id!r} must reference three distinct atoms"
            )
        object.__setattr__(self, "symmetry_metadata", dict(self.symmetry_metadata))

    def to_dict(self, *, atom_index_base: int = 0) -> dict[str, object]:
        return {
            "id": self.bond_id,
            "donor_atom": self.donor_atom + atom_index_base,
            "hydrogen_atom": self.hydrogen_atom + atom_index_base,
            "acceptor_atom": self.acceptor_atom + atom_index_base,
            **dict(self.symmetry_metadata),
        }


@dataclass(frozen=True)
class HofSystem:
    """Validated HOF dimer identity and its explicit chemical partition."""

    system_id: str
    label: str
    structure_path: Path
    geometry: XYZGeometry
    atom_index_base: int
    charge: int
    multiplicity: int
    fragments: tuple[HofFragment, ...]
    hydrogen_bonds: tuple[HofHydrogenBond, ...]
    expected_hydrogen_bonds: int
    symmetry_equivalent: bool

    def __post_init__(self) -> None:
        if not self.system_id:
            raise ValueError("HOF system id is required")
        if self.atom_index_base not in {0, 1}:
            raise ValueError("atom_index_base must be 0 or 1")
        if self.multiplicity < 1:
            raise ValueError("system multiplicity must be positive")
        if self.expected_hydrogen_bonds < 0:
            raise ValueError("expected hydrogen-bond count must be non-negative")
        object.__setattr__(self, "structure_path", Path(self.structure_path))
        object.__setattr__(self, "fragments", tuple(self.fragments))
        object.__setattr__(self, "hydrogen_bonds", tuple(self.hydrogen_bonds))

    @property
    def fragment_map(self) -> dict[str, HofFragment]:
        return {fragment.fragment_id: fragment for fragment in self.fragments}

    @property
    def atom_to_fragment(self) -> dict[int, str]:
        return {
            atom_index: fragment.fragment_id
            for fragment in self.fragments
            for atom_index in fragment.atom_indices
        }

    @property
    def system_identity(self) -> str:
        return stable_hash(
            {
                "schema_version": HOF_ADAPTER_SCHEMA_VERSION,
                "adapter": "hof",
                "system_id": self.system_id,
                "geometry_sha256": geometry_hash(self.geometry),
                "charge": self.charge,
                "multiplicity": self.multiplicity,
                "fragments": [fragment.to_dict() for fragment in self.fragments],
                "hydrogen_bonds": [bond.to_dict() for bond in self.hydrogen_bonds],
                "expected_hydrogen_bonds": self.expected_hydrogen_bonds,
                "symmetry_equivalent": self.symmetry_equivalent,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": HOF_ADAPTER_SCHEMA_VERSION,
            "system_id": self.system_id,
            "system_identity": self.system_identity,
            "label": self.label,
            "structure_path": str(self.structure_path),
            "geometry_sha256": geometry_hash(self.geometry),
            "atom_count": self.geometry.atom_count,
            "atom_index_base": self.atom_index_base,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "fragments": [
                fragment.to_dict(atom_index_base=self.atom_index_base)
                for fragment in self.fragments
            ],
            "hydrogen_bonds": {
                "expected_count": self.expected_hydrogen_bonds,
                "symmetry_equivalent": self.symmetry_equivalent,
                "bonds": [
                    bond.to_dict(atom_index_base=self.atom_index_base)
                    for bond in self.hydrogen_bonds
                ],
            },
        }


@dataclass(frozen=True)
class HofInteractionProtocol:
    """Resolved HOF interaction-energy intent for an ORCA adapter."""

    method_ref: str
    program: str
    method: str
    basis: str
    pno: str
    tight_scf: bool
    counterpoise: bool
    led: bool
    frozen_fragments: bool
    geometry_source: str
    calculate_deformation_energy: bool
    outputs: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.method_ref, self.program, self.method, self.basis, self.pno)):
            raise ValueError(
                "interaction protocol requires program, method, basis, and PNO"
            )
        if not self.geometry_source:
            raise ValueError("interaction protocol geometry_source is required")
        if len(set(self.outputs)) != len(self.outputs):
            raise ValueError("interaction protocol outputs must be unique")
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def energy_metadata(self) -> dict[str, object]:
        """Settings that define comparable dimer and fragment energies."""

        return {
            "method": self.method,
            "basis": self.basis,
            "pno": self.pno,
            "tight_scf": self.tight_scf,
            "counterpoise": self.counterpoise,
            "frozen_fragments": self.frozen_fragments,
        }

    @property
    def led_metadata(self) -> dict[str, object]:
        return {
            **self.energy_metadata,
            "led": self.led,
            "fragments_required": True,
            "expected_fragments": 2,
        }

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HofStartingGeometryProtocol:
    """Declared provenance for the source structure selected by the HOF project."""

    source: str

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("starting geometry source is required")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HofGeometryProtocol:
    """Resolved HOF geometry-optimization intent."""

    method_ref: str
    program: str
    method: str
    task: str
    outputs: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.method_ref, self.program, self.method, self.task)):
            raise ValueError("geometry protocol requires method, program, and task")
        if len(set(self.outputs)) != len(self.outputs):
            raise ValueError("geometry protocol outputs must be unique")
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def method_metadata(self) -> dict[str, object]:
        return {
            "method": self.method,
            "task": self.task,
            **dict(self.metadata),
        }

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HofIgmhProtocol:
    """Resolved density-calculation and generic Multiwfn IGMH intent."""

    density_method_ref: str
    program: str
    method: str
    basis: str
    tight_scf: bool
    task: str
    geometry_source: str
    outputs: tuple[str, ...]
    profile: str = "interfragment"
    cube_generation: bool = True
    grid_spacing_bohr: float | None = None
    visualization: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.density_method_ref,
                self.program,
                self.method,
                self.basis,
                self.task,
                self.geometry_source,
            )
        ):
            raise ValueError("IGMH protocol requires a complete density method")
        if self.profile != "interfragment":
            raise ValueError("HOF IGMH currently requires the interfragment profile")
        if not self.cube_generation:
            raise ValueError("HOF IGMH requires cube generation")
        if self.grid_spacing_bohr is not None and self.grid_spacing_bohr <= 0:
            raise ValueError("IGMH grid spacing must be positive when provided")
        if len(set(self.outputs)) != len(self.outputs):
            raise ValueError("IGMH protocol outputs must be unique")
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "visualization", dict(self.visualization))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def density_metadata(self) -> dict[str, object]:
        return {
            "method": self.method,
            "basis": self.basis,
            "tight_scf": self.tight_scf,
            "task": self.task,
        }

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HofAdapterConfiguration:
    """Validated HOF protocols ready for adapter-level workflow translation."""

    system: HofSystem
    interaction: HofInteractionProtocol
    starting_geometry: HofStartingGeometryProtocol | None = None
    geometry: HofGeometryProtocol | None = None
    igmh: HofIgmhProtocol | None = None
    source_files: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_files", dict(self.source_files))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": HOF_ADAPTER_SCHEMA_VERSION,
            "adapter": "hof",
            "system": self.system.to_dict(),
            "interaction": self.interaction.to_dict(),
            "starting_geometry": (
                self.starting_geometry.to_dict()
                if self.starting_geometry is not None
                else None
            ),
            "geometry": self.geometry.to_dict() if self.geometry is not None else None,
            "igmh": self.igmh.to_dict() if self.igmh is not None else None,
            "source_files": dict(self.source_files),
        }


__all__ = [
    "HOF_ADAPTER_SCHEMA_VERSION",
    "HofAdapterConfiguration",
    "HofFragment",
    "HofGeometryProtocol",
    "HofHydrogenBond",
    "HofIgmhProtocol",
    "HofInteractionProtocol",
    "HofStartingGeometryProtocol",
    "HofSystem",
]
