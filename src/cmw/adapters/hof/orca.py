"""Fragment-aware ORCA input contracts for the HOF adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from cmw.core.provenance import stable_hash
from cmw.molecular.orca.input import (
    OrcaResources,
    OrcaStageSpec,
    resolve_orca_resources,
    validate_orca_execution_contract,
)
from cmw.molecular.orca.status import StageType

from .models import HofAdapterConfiguration, HofSystem


@dataclass(frozen=True)
class HofOrcaCalculation:
    """One non-executing ORCA input plan for a HOF workflow node."""

    calculation_id: str
    node_id: str
    role: str
    charge: int
    multiplicity: int
    spec: OrcaStageSpec
    active_atom_indices: tuple[int, ...]
    ghost_atom_indices: tuple[int, ...] = ()
    fragment_id: str | None = None
    resources: OrcaResources | None = None
    execution: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.calculation_id, self.node_id, self.role)):
            raise ValueError(
                "HOF ORCA calculation identity, node, and role are required"
            )
        if self.multiplicity < 1:
            raise ValueError("HOF ORCA calculation multiplicity must be positive")
        if set(self.active_atom_indices).intersection(self.ghost_atom_indices):
            raise ValueError("active and ghost atom sets must not overlap")
        object.__setattr__(self, "active_atom_indices", tuple(self.active_atom_indices))
        object.__setattr__(self, "ghost_atom_indices", tuple(self.ghost_atom_indices))
        object.__setattr__(self, "execution", dict(self.execution))

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "calculation_id": self.calculation_id,
            "node_id": self.node_id,
            "role": self.role,
            "fragment_id": self.fragment_id,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "active_atom_indices": list(self.active_atom_indices),
            "ghost_atom_indices": list(self.ghost_atom_indices),
            "scientific_identity": self.spec.scientific_identity(),
            "execution_intent": self.spec.execution_intent.to_dict(),
        }
        if self.resources is not None:
            value["resources"] = self.resources.to_dict()
            value["execution"] = dict(self.execution)
        return value


def _keywords(configuration: HofAdapterConfiguration, *, led: bool) -> str:
    protocol = configuration.interaction
    tokens = [protocol.method, protocol.basis, protocol.pno]
    if protocol.tight_scf:
        tokens.append("TightSCF")
    if led:
        tokens.append("LED")
    return " ".join(tokens)


def _calculation_id(
    configuration: HofAdapterConfiguration,
    *,
    node_id: str,
    role: str,
    fragment_id: str | None = None,
) -> str:
    return stable_hash(
        {
            "adapter": "hof",
            "system_identity": configuration.system.system_identity,
            "node_id": node_id,
            "role": role,
            "fragment_id": fragment_id,
            "energy_protocol": configuration.interaction.energy_metadata,
        }
    )


def build_hof_orca_calculations(
    configuration: HofAdapterConfiguration,
    *,
    dimer_node_id: str = "dimer",
    fragment_node_ids: Mapping[str, str] | None = None,
) -> dict[str, HofOrcaCalculation]:
    """Build dimer/ghost-fragment ORCA plans without launching ORCA."""

    system = configuration.system
    node_ids = dict(
        fragment_node_ids
        or {
            fragment.fragment_id: f"fragment_{fragment.fragment_id.casefold()}"
            for fragment in system.fragments
        }
    )
    if set(node_ids) != set(system.fragment_map):
        raise ValueError("fragment node ids must cover every configured fragment")
    all_atoms = tuple(range(system.geometry.atom_count))
    resolved_resources = (
        resolve_orca_resources(configuration.execution_profile)
        if configuration.execution_profile is not None
        else None
    )
    resources = resolved_resources.resources if resolved_resources is not None else None
    execution = resolved_resources.to_dict() if resolved_resources is not None else {}
    calculations: dict[str, HofOrcaCalculation] = {}
    dimer_protocol = {
        **configuration.interaction.led_metadata,
        "adapter": "hof",
        "system_identity": system.system_identity,
    }
    calculations[dimer_node_id] = HofOrcaCalculation(
        calculation_id=_calculation_id(
            configuration, node_id=dimer_node_id, role="dimer"
        ),
        node_id=dimer_node_id,
        role="dimer",
        charge=system.charge,
        multiplicity=system.multiplicity,
        spec=OrcaStageSpec(
            StageType.SP,
            _keywords(configuration, led=True),
            protocol=dimer_protocol,
        ),
        active_atom_indices=all_atoms,
        resources=resources,
        execution=execution,
    )
    for fragment in system.fragments:
        node_id = node_ids[fragment.fragment_id]
        active = tuple(fragment.atom_indices)
        active_set = set(active)
        ghosts = tuple(index for index in all_atoms if index not in active_set)
        fragment_protocol = {
            **configuration.interaction.energy_metadata,
            "adapter": "hof",
            "system_identity": system.system_identity,
            "led": False,
            "fragments_required": False,
            "fragment_id": fragment.fragment_id,
            "active_atom_count": len(active),
            "ghost_atom_count": len(ghosts),
        }
        calculations[node_id] = HofOrcaCalculation(
            calculation_id=_calculation_id(
                configuration,
                node_id=node_id,
                role="cp_fragment",
                fragment_id=fragment.fragment_id,
            ),
            node_id=node_id,
            role="cp_fragment",
            fragment_id=fragment.fragment_id,
            charge=fragment.charge,
            multiplicity=fragment.multiplicity,
            spec=OrcaStageSpec(
                StageType.SP,
                _keywords(configuration, led=False),
                protocol=fragment_protocol,
            ),
            active_atom_indices=active,
            ghost_atom_indices=ghosts,
            resources=resources,
            execution=execution,
        )
    return calculations


def render_hof_orca_input(
    *,
    system: HofSystem,
    calculation: HofOrcaCalculation,
    resources: OrcaResources | None = None,
) -> str:
    """Render inline fragment labels for LED or ghost atoms for CP fragments."""

    atom_count = system.geometry.atom_count
    active = set(calculation.active_atom_indices)
    ghosts = set(calculation.ghost_atom_indices)
    if active.union(ghosts) != set(range(atom_count)):
        raise ValueError("HOF ORCA atom partition must cover the full dimer geometry")
    if calculation.role == "dimer" and ghosts:
        raise ValueError("dimer ORCA calculation cannot contain ghost atoms")
    if calculation.role == "cp_fragment" and not ghosts:
        raise ValueError("counterpoise fragment calculation requires ghost atoms")
    selected_resources = resources or calculation.resources
    if selected_resources is None:
        raise ValueError("HOF ORCA rendering requires explicit execution resources")

    keyword_tokens = " ".join(calculation.spec.keywords.split())
    intent = calculation.spec.execution_intent
    validate_orca_execution_contract(intent)
    lines = [
        f"! {keyword_tokens} {intent.required_behavior}",
        f"%pal nprocs {selected_resources.nprocs} end",
        f"%maxcore {selected_resources.maxcore_mb_per_process}",
    ]
    lines.extend(
        str(block) for block in calculation.spec.scientific_identity()["blocks"]
    )
    lines.append(f"* xyz {calculation.charge} {calculation.multiplicity}")
    fragment_numbers = {
        fragment.fragment_id: number
        for number, fragment in enumerate(system.fragments, start=1)
    }
    atom_fragments = system.atom_to_fragment
    for index, atom in enumerate(system.geometry.atoms):
        if calculation.role == "dimer":
            symbol = f"{atom.element}({fragment_numbers[atom_fragments[index]]})"
        else:
            symbol = f"{atom.element}:" if index in ghosts else atom.element
        lines.append(f"{symbol:<8s} {atom.x: .12f} {atom.y: .12f} {atom.z: .12f}")
    lines.append("*")
    validate_orca_execution_contract(intent, rendered_behavior=lines[0].split()[-1])
    return "\n".join(lines) + "\n"


__all__ = [
    "HofOrcaCalculation",
    "build_hof_orca_calculations",
    "render_hof_orca_input",
]
