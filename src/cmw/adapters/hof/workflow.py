"""Translate validated HOF configuration into generic CMW workflow contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping

from cmw.core.artifacts import (
    Artifact,
    CPInteractionArtifact,
    DimerEnergyArtifact,
    FragmentEnergyArtifact,
    LEDArtifact,
    StructureArtifact,
    WavefunctionArtifact,
)
from cmw.core.provenance import stable_hash
from cmw.core.workflow_graph import (
    AggregationNode,
    ArtifactRequirement,
    CalculationNode,
    DerivedResultNode,
    WorkflowGraph,
)

from .models import HofAdapterConfiguration
from .orca import HofOrcaCalculation, build_hof_orca_calculations


def _node_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not token:
        raise ValueError(f"fragment id cannot form a workflow node id: {value!r}")
    return token


@dataclass(frozen=True)
class HofInteractionPlan:
    """Non-executing HOF translation result: DAG, artifacts, and ORCA plans."""

    configuration: HofAdapterConfiguration
    graph: WorkflowGraph
    artifact_templates: Mapping[str, tuple[Artifact, ...]]
    orca_calculations: Mapping[str, HofOrcaCalculation]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_templates",
            {key: tuple(value) for key, value in self.artifact_templates.items()},
        )
        object.__setattr__(self, "orca_calculations", dict(self.orca_calculations))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "adapter": "hof",
            "configuration": self.configuration.to_dict(),
            "workflow_graph": self.graph.to_dict(),
            "artifact_templates": {
                node_id: [artifact.to_dict() for artifact in artifacts]
                for node_id, artifacts in self.artifact_templates.items()
            },
            "orca_calculations": {
                node_id: calculation.to_dict()
                for node_id, calculation in self.orca_calculations.items()
            },
            "metadata": dict(self.metadata),
        }


def build_hof_interaction_workflow(
    configuration: HofAdapterConfiguration,
    *,
    structure_artifact: StructureArtifact | None = None,
) -> HofInteractionPlan:
    """Build dimer -> fragments -> CP interaction -> LED with generic CMW types."""

    system = configuration.system
    fragment_node_ids = {
        fragment.fragment_id: f"fragment_{_node_token(fragment.fragment_id)}"
        for fragment in system.fragments
    }
    if len(set(fragment_node_ids.values())) != len(fragment_node_ids):
        raise ValueError("fragment ids produce duplicate workflow node ids")
    calculations = build_hof_orca_calculations(
        configuration,
        structure_artifact=structure_artifact,
        fragment_node_ids=fragment_node_ids,
    )
    effective_structure_artifact = calculations["dimer"].geometry_artifact
    dimer_id = "dimer"
    cp_id = "cp_interaction"
    led_id = "led"
    fragment_nodes = tuple(fragment_node_ids.values())
    dimer_calculation = calculations[dimer_id]

    nodes = [
        CalculationNode(
            dimer_id,
            operation="orca_single_point",
            role="supersystem",
            requires=(ArtifactRequirement("StructureArtifact"),),
            produces=("DimerEnergyArtifact", "WavefunctionArtifact"),
            configuration={
                "calculation_id": dimer_calculation.calculation_id,
                "system_identity": system.system_identity,
                "method": configuration.interaction.method,
                "basis": configuration.interaction.basis,
                "protocol": dict(dimer_calculation.spec.protocol),
                "execution_intent": dimer_calculation.spec.execution_intent.to_dict(),
            },
        )
    ]
    for fragment in system.fragments:
        node_id = fragment_node_ids[fragment.fragment_id]
        calculation = calculations[node_id]
        nodes.append(
            CalculationNode(
                node_id,
                dependencies=(dimer_id,),
                operation="orca_single_point",
                role="cp_fragment",
                produces=("FragmentEnergyArtifact",),
                configuration={
                    "calculation_id": calculation.calculation_id,
                    "fragment_id": fragment.fragment_id,
                    "charge": fragment.charge,
                    "multiplicity": fragment.multiplicity,
                    "active_atom_indices": list(fragment.atom_indices),
                    "ghost_atom_indices": list(calculation.ghost_atom_indices),
                    "protocol": dict(calculation.spec.protocol),
                    "execution_intent": calculation.spec.execution_intent.to_dict(),
                },
            )
        )
    nodes.extend(
        [
            AggregationNode(
                cp_id,
                dependencies=(dimer_id, *fragment_nodes),
                operation="counterpoise_interaction",
                requires=(
                    ArtifactRequirement("DimerEnergyArtifact", 1, (dimer_id,)),
                    ArtifactRequirement(
                        "FragmentEnergyArtifact", len(fragment_nodes), fragment_nodes
                    ),
                ),
                produces=("CPInteractionArtifact",),
                configuration={
                    "equation": "E_supersystem - sum(E_fragment_i)",
                    "system_identity": system.system_identity,
                },
            ),
            DerivedResultNode(
                led_id,
                dependencies=(cp_id, dimer_id),
                operation="local_energy_decomposition",
                requires=(
                    ArtifactRequirement("CPInteractionArtifact", 1, (cp_id,)),
                    ArtifactRequirement("WavefunctionArtifact", 1, (dimer_id,)),
                ),
                produces=("LEDArtifact",),
                configuration={
                    "source_calculation_id": dimer_calculation.calculation_id,
                    "system_identity": system.system_identity,
                },
            ),
        ]
    )
    graph = WorkflowGraph(
        f"hof_interaction_{_node_token(system.system_id)}_{system.system_identity[:12]}",
        tuple(nodes),
        external_inputs=("StructureArtifact",),
    )

    provenance = {
        "adapter": "cmw.adapters.hof",
        "system_identity": system.system_identity,
        "source_files": dict(configuration.source_files),
    }
    method = configuration.interaction.method
    basis = configuration.interaction.basis
    energy_protocol = configuration.interaction.energy_metadata
    structure_parents = (effective_structure_artifact.artifact_id,)
    dimer_energy = DimerEnergyArtifact(
        producing_calculation=dimer_calculation.calculation_id,
        method=method,
        basis=basis,
        protocol=energy_protocol,
        parent_artifacts=structure_parents,
        provenance=provenance,
        metadata={"energy_role": "supersystem", "system_id": system.system_id},
    )
    wavefunction = WavefunctionArtifact(
        producing_calculation=dimer_calculation.calculation_id,
        method=method,
        basis=basis,
        protocol=configuration.interaction.led_metadata,
        parent_artifacts=structure_parents,
        provenance=provenance,
        metadata={"analysis_role": "led_source", "system_id": system.system_id},
    )
    artifact_templates: dict[str, tuple[Artifact, ...]] = {
        dimer_id: (dimer_energy, wavefunction)
    }
    fragment_energies: list[FragmentEnergyArtifact] = []
    for fragment in system.fragments:
        node_id = fragment_node_ids[fragment.fragment_id]
        artifact = FragmentEnergyArtifact(
            producing_calculation=calculations[node_id].calculation_id,
            method=method,
            basis=basis,
            protocol=energy_protocol,
            parent_artifacts=(dimer_energy.artifact_id,),
            provenance=provenance,
            metadata={
                "energy_role": "fragment",
                "fragment_id": fragment.fragment_id,
                "system_id": system.system_id,
            },
        )
        fragment_energies.append(artifact)
        artifact_templates[node_id] = (artifact,)
    cp_operation_id = stable_hash(
        {
            "adapter": "hof",
            "operation": "counterpoise_interaction",
            "system_identity": system.system_identity,
            "parents": [
                dimer_energy.artifact_id,
                *(artifact.artifact_id for artifact in fragment_energies),
            ],
        }
    )
    cp_artifact = CPInteractionArtifact(
        producing_calculation=cp_operation_id,
        method=method,
        basis=basis,
        protocol=energy_protocol,
        parent_artifacts=(
            dimer_energy.artifact_id,
            *(artifact.artifact_id for artifact in fragment_energies),
        ),
        provenance=provenance,
        metadata={
            "derived_quantity": "E_supersystem - sum(E_fragment_i)",
            "system_id": system.system_id,
        },
    )
    artifact_templates[cp_id] = (cp_artifact,)
    led_artifact = LEDArtifact(
        producing_calculation=stable_hash(
            {
                "adapter": "hof",
                "operation": "led",
                "source_calculation": dimer_calculation.calculation_id,
                "interaction_artifact": cp_artifact.artifact_id,
            }
        ),
        method=method,
        basis=basis,
        protocol=configuration.interaction.led_metadata,
        parent_artifacts=(wavefunction.artifact_id, cp_artifact.artifact_id),
        provenance=provenance,
        metadata={"system_id": system.system_id},
    )
    artifact_templates[led_id] = (led_artifact,)
    return HofInteractionPlan(
        configuration=configuration,
        graph=graph,
        artifact_templates=artifact_templates,
        orca_calculations=calculations,
        metadata={
            "requested_outputs": list(configuration.interaction.outputs),
            "calculate_deformation_energy": configuration.interaction.calculate_deformation_energy,
        },
    )


__all__ = ["HofInteractionPlan", "build_hof_interaction_workflow"]
