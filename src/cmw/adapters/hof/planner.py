"""Compose the complete HOF calculation plan from generic CMW workflows."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Mapping

from cmw.core.artifacts import (
    Artifact,
    ArtifactValidation,
    DeformationEnergyArtifact,
    DensityArtifact,
    FragmentEnergyArtifact,
    IGMHArtifact,
    OptimizationArtifact,
    StructureArtifact,
    ValidationStatus,
    artifact_matches_type,
)
from cmw.core.provenance import stable_hash
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.core.workflow_graph import (
    AggregationNode,
    ArtifactBinding,
    ArtifactRequirement,
    CalculationNode,
    WorkflowGraph,
)
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.status import StageType
from cmw.molecular.workflows.igmh import default_igmh_outputs
from cmw.molecular.workflows.multiwfn_analysis import (
    AnalysisOperation,
    analysis_workflow_graph,
)

from .models import HofAdapterConfiguration, HofFragment
from .orca import HofOrcaCalculation
from .workflow import _node_token, build_hof_interaction_workflow


PLANNED = ArtifactValidation(
    ValidationStatus.UNKNOWN,
    {"planned": True, "executed": False},
    "PLANNED_NOT_EXECUTED",
    "calculation or analysis has been planned but not executed",
)


@dataclass(frozen=True)
class HofWorkflowPlan:
    """Complete non-executing HOF DAG with typed artifact lineage."""

    configuration: HofAdapterConfiguration
    graph: WorkflowGraph
    artifact_templates: Mapping[str, tuple[Artifact, ...]]
    orca_calculations: Mapping[str, HofOrcaCalculation]
    multiwfn_plans: Mapping[str, Mapping[str, object]]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_templates",
            {node_id: tuple(items) for node_id, items in self.artifact_templates.items()},
        )
        object.__setattr__(self, "orca_calculations", dict(self.orca_calculations))
        object.__setattr__(
            self,
            "multiwfn_plans",
            {node_id: dict(value) for node_id, value in self.multiwfn_plans.items()},
        )
        object.__setattr__(self, "metadata", dict(self.metadata))
        _validate_complete_plan(self)

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
            "multiwfn_plans": {
                node_id: dict(value) for node_id, value in self.multiwfn_plans.items()
            },
            "metadata": dict(self.metadata),
        }


def _require_complete_configuration(configuration: HofAdapterConfiguration) -> None:
    missing = [
        name
        for name, value in (
            ("starting_geometry", configuration.starting_geometry),
            ("geometry", configuration.geometry),
            ("igmh", configuration.igmh),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "complete HOF workflow is missing protocol sections: " + ", ".join(missing)
        )
    if not configuration.interaction.calculate_deformation_energy:
        raise ValueError("complete HOF workflow requires deformation-energy planning")


def _provenance(configuration: HofAdapterConfiguration) -> dict[str, object]:
    return {
        "adapter": "cmw.adapters.hof",
        "system_identity": configuration.system.system_identity,
        "source_files": dict(configuration.source_files),
        "execution": "planned_only",
    }


def _calculation_id(
    configuration: HofAdapterConfiguration,
    *,
    node_id: str,
    role: str,
    protocol: Mapping[str, object],
) -> str:
    return stable_hash(
        {
            "adapter": "hof",
            "system_identity": configuration.system.system_identity,
            "node_id": node_id,
            "role": role,
            "protocol": dict(protocol),
        }
    )


def _orca_plan(
    configuration: HofAdapterConfiguration,
    *,
    node_id: str,
    role: str,
    stage_type: StageType,
    keywords: str,
    protocol: Mapping[str, object],
    fragment: HofFragment | None = None,
) -> HofOrcaCalculation:
    system = configuration.system
    active = (
        fragment.atom_indices
        if fragment is not None
        else tuple(range(system.geometry.atom_count))
    )
    calculation_id = _calculation_id(
        configuration, node_id=node_id, role=role, protocol=protocol
    )
    return HofOrcaCalculation(
        calculation_id=calculation_id,
        node_id=node_id,
        role=role,
        fragment_id=fragment.fragment_id if fragment is not None else None,
        charge=fragment.charge if fragment is not None else system.charge,
        multiplicity=(
            fragment.multiplicity if fragment is not None else system.multiplicity
        ),
        spec=OrcaStageSpec(stage_type, keywords, protocol=protocol),
        active_atom_indices=active,
    )


def _geometry_branch(
    configuration: HofAdapterConfiguration,
) -> tuple[
    WorkflowGraph,
    dict[str, tuple[Artifact, ...]],
    dict[str, HofOrcaCalculation],
    StructureArtifact,
]:
    geometry_protocol = configuration.geometry
    starting = configuration.starting_geometry
    assert geometry_protocol is not None and starting is not None
    system = configuration.system
    provenance = _provenance(configuration)
    input_structure = structure_artifact_from_file(
        system.structure_path,
        source=starting.source,
        producing_calculation=stable_hash(
            {
                "adapter": "hof",
                "operation": "structure_import",
                "system_identity": system.system_identity,
            }
        ),
        charge=system.charge,
        multiplicity=system.multiplicity,
        provenance=provenance,
        metadata={"system_id": system.system_id, "structure_role": "input"},
    )
    keywords = geometry_protocol.method
    protocol = {
        **geometry_protocol.method_metadata,
        "adapter": "hof",
        "system_identity": system.system_identity,
        "frequency_requested": geometry_protocol.task.casefold() == "opt_freq",
        "outputs": list(geometry_protocol.outputs),
    }
    calculation = _orca_plan(
        configuration,
        node_id="geometry_optimization",
        role="geometry_optimization",
        stage_type=StageType.OPT,
        keywords=keywords,
        protocol=protocol,
    )
    optimization = OptimizationArtifact(
        producing_calculation=calculation.calculation_id,
        method=geometry_protocol.method,
        protocol=protocol,
        parent_artifacts=(input_structure.artifact_id,),
        files={"optimized_structure": "geometry/optimized_geometry.xyz"},
        validation=PLANNED,
        provenance=provenance,
        metadata={"system_id": system.system_id},
    )
    optimized_structure = StructureArtifact(
        producing_calculation=calculation.calculation_id,
        method=geometry_protocol.method,
        protocol=protocol,
        parent_artifacts=(optimization.artifact_id,),
        files={"structure": "geometry/optimized_geometry.xyz"},
        validation=PLANNED,
        provenance=provenance,
        metadata={"system_id": system.system_id, "structure_role": "optimized"},
        source="geometry_optimization",
        format=input_structure.format,
        atom_count=input_structure.atom_count,
        elemental_composition=input_structure.elemental_composition,
        charge=input_structure.charge,
        multiplicity=input_structure.multiplicity,
        geometry_hash="",
    )
    graph = WorkflowGraph(
        f"hof_geometry_{_node_token(system.system_id)}",
        (
            CalculationNode(
                "input_structure",
                role="structure_input",
                produces=("StructureArtifact",),
                configuration={"artifact_id": input_structure.artifact_id},
            ),
            CalculationNode(
                "geometry_optimization",
                dependencies=("input_structure",),
                operation="orca_optimization",
                role="geometry_optimization",
                requires=(
                    ArtifactRequirement("StructureArtifact", 1, ("input_structure",)),
                ),
                produces=("OptimizationArtifact", "StructureArtifact"),
                configuration={"calculation_id": calculation.calculation_id, **protocol},
            ),
        ),
        provenance={"adapter_branch": "geometry"},
    )
    return (
        graph,
        {
            "input_structure": (input_structure,),
            "geometry_optimization": (optimization, optimized_structure),
        },
        {"geometry_optimization": calculation},
        optimized_structure,
    )


def _deformation_branch(
    configuration: HofAdapterConfiguration, optimized_structure: StructureArtifact
) -> tuple[WorkflowGraph, dict[str, tuple[Artifact, ...]], dict[str, HofOrcaCalculation]]:
    geometry_protocol = configuration.geometry
    assert geometry_protocol is not None
    system = configuration.system
    provenance = _provenance(configuration)
    nodes: list[CalculationNode | AggregationNode] = []
    artifacts: dict[str, tuple[Artifact, ...]] = {}
    calculations: dict[str, HofOrcaCalculation] = {}
    energy_artifacts: list[FragmentEnergyArtifact] = []
    energy_nodes: list[str] = []
    energy_keywords = " ".join(
        filter(
            None,
            (
                configuration.interaction.method,
                configuration.interaction.basis,
                configuration.interaction.pno,
                "TightSCF" if configuration.interaction.tight_scf else "",
            ),
        )
    )

    for fragment in system.fragments:
        token = _node_token(fragment.fragment_id)
        distorted_id = f"distorted_fragment_{token}_energy"
        relax_id = f"relax_fragment_{token}"
        relaxed_id = f"relaxed_fragment_{token}_energy"
        energy_protocol = {
            **configuration.interaction.energy_metadata,
            "counterpoise": False,
            "led": False,
            "deformation_energy": True,
            "fragment_id": fragment.fragment_id,
        }
        distorted_plan = _orca_plan(
            configuration,
            node_id=distorted_id,
            role="distorted_fragment_energy",
            stage_type=StageType.SP,
            keywords=energy_keywords,
            protocol={**energy_protocol, "geometry_role": "distorted"},
            fragment=fragment,
        )
        relaxation_protocol = {
            **geometry_protocol.method_metadata,
            "adapter": "hof",
            "system_identity": system.system_identity,
            "fragment_id": fragment.fragment_id,
            "geometry_role": "relaxed_isolated_fragment",
        }
        relaxation_plan = _orca_plan(
            configuration,
            node_id=relax_id,
            role="isolated_fragment_optimization",
            stage_type=StageType.OPT,
            keywords=geometry_protocol.method,
            protocol=relaxation_protocol,
            fragment=fragment,
        )
        relaxed_plan = _orca_plan(
            configuration,
            node_id=relaxed_id,
            role="relaxed_fragment_energy",
            stage_type=StageType.SP,
            keywords=energy_keywords,
            protocol={**energy_protocol, "geometry_role": "relaxed"},
            fragment=fragment,
        )
        calculations.update(
            {
                distorted_id: distorted_plan,
                relax_id: relaxation_plan,
                relaxed_id: relaxed_plan,
            }
        )
        nodes.extend(
            (
                CalculationNode(
                    distorted_id,
                    operation="orca_single_point",
                    role="distorted_fragment_energy",
                    requires=(ArtifactRequirement("StructureArtifact"),),
                    produces=("FragmentEnergyArtifact",),
                    configuration={"calculation_id": distorted_plan.calculation_id},
                ),
                CalculationNode(
                    relax_id,
                    operation="orca_optimization",
                    role="isolated_fragment_optimization",
                    requires=(ArtifactRequirement("StructureArtifact"),),
                    produces=("OptimizationArtifact", "StructureArtifact"),
                    configuration={"calculation_id": relaxation_plan.calculation_id},
                ),
                CalculationNode(
                    relaxed_id,
                    dependencies=(relax_id,),
                    operation="orca_single_point",
                    role="relaxed_fragment_energy",
                    requires=(
                        ArtifactRequirement("StructureArtifact", 1, (relax_id,)),
                    ),
                    produces=("FragmentEnergyArtifact",),
                    configuration={"calculation_id": relaxed_plan.calculation_id},
                ),
            )
        )
        distorted_artifact = FragmentEnergyArtifact(
            producing_calculation=distorted_plan.calculation_id,
            method=configuration.interaction.method,
            basis=configuration.interaction.basis,
            protocol={**energy_protocol, "geometry_role": "distorted"},
            parent_artifacts=(optimized_structure.artifact_id,),
            validation=PLANNED,
            provenance=provenance,
            metadata={
                "fragment_id": fragment.fragment_id,
                "energy_role": "distorted",
                "geometry_state": "distorted",
                "charge": fragment.charge,
                "multiplicity": fragment.multiplicity,
            },
        )
        relaxation_artifact = OptimizationArtifact(
            producing_calculation=relaxation_plan.calculation_id,
            method=geometry_protocol.method,
            protocol=relaxation_protocol,
            parent_artifacts=(optimized_structure.artifact_id,),
            files={"optimized_structure": f"deformation/{token}_relaxed.xyz"},
            validation=PLANNED,
            provenance=provenance,
            metadata={"fragment_id": fragment.fragment_id},
        )
        relaxed_structure = StructureArtifact(
            producing_calculation=relaxation_plan.calculation_id,
            method=geometry_protocol.method,
            protocol=relaxation_protocol,
            parent_artifacts=(relaxation_artifact.artifact_id,),
            files={"structure": f"deformation/{token}_relaxed.xyz"},
            validation=PLANNED,
            provenance=provenance,
            metadata={"fragment_id": fragment.fragment_id, "structure_role": "relaxed"},
            source="isolated_fragment_optimization",
            format=optimized_structure.format,
            atom_count=len(fragment.atom_indices),
            elemental_composition=dict(
                Counter(
                    system.geometry.atoms[index].element for index in fragment.atom_indices
                )
            ),
            charge=fragment.charge,
            multiplicity=fragment.multiplicity,
            geometry_hash="",
        )
        relaxed_artifact = FragmentEnergyArtifact(
            producing_calculation=relaxed_plan.calculation_id,
            method=configuration.interaction.method,
            basis=configuration.interaction.basis,
            protocol={**energy_protocol, "geometry_role": "relaxed"},
            parent_artifacts=(relaxed_structure.artifact_id,),
            validation=PLANNED,
            provenance=provenance,
            metadata={
                "fragment_id": fragment.fragment_id,
                "energy_role": "relaxed",
                "geometry_state": "relaxed",
                "charge": fragment.charge,
                "multiplicity": fragment.multiplicity,
            },
        )
        artifacts[distorted_id] = (distorted_artifact,)
        artifacts[relax_id] = (relaxation_artifact, relaxed_structure)
        artifacts[relaxed_id] = (relaxed_artifact,)
        energy_artifacts.extend((distorted_artifact, relaxed_artifact))
        energy_nodes.extend((distorted_id, relaxed_id))

    deformation_id = "deformation_energy"
    nodes.append(
        AggregationNode(
            deformation_id,
            dependencies=tuple(energy_nodes),
            operation="deformation_energy_difference",
            requires=(
                ArtifactRequirement(
                    "FragmentEnergyArtifact", len(energy_nodes), tuple(energy_nodes)
                ),
            ),
            produces=("DeformationEnergyArtifact",),
            configuration={
                "equation": "sum(E_distorted_fragment_i - E_relaxed_fragment_i)",
                "fragment_count": len(system.fragments),
            },
        )
    )
    artifacts[deformation_id] = (
        DeformationEnergyArtifact(
            producing_calculation=stable_hash(
                {
                    "adapter": "hof",
                    "operation": "deformation_energy",
                    "parents": [item.artifact_id for item in energy_artifacts],
                }
            ),
            method=configuration.interaction.method,
            basis=configuration.interaction.basis,
            protocol={
                **configuration.interaction.energy_metadata,
                "counterpoise": False,
            },
            parent_artifacts=tuple(item.artifact_id for item in energy_artifacts),
            validation=PLANNED,
            provenance=provenance,
            metadata={
                "derived_quantity": "sum(E_distorted_fragment_i - E_relaxed_fragment_i)",
                "system_id": system.system_id,
            },
        ),
    )
    return (
        WorkflowGraph(
            f"hof_deformation_{_node_token(system.system_id)}",
            tuple(nodes),
            external_inputs=("StructureArtifact",),
            provenance={"adapter_branch": "deformation"},
        ),
        artifacts,
        calculations,
    )


def _density_igmh_branch(
    configuration: HofAdapterConfiguration, optimized_structure: StructureArtifact
) -> tuple[
    WorkflowGraph,
    WorkflowGraph,
    dict[str, tuple[Artifact, ...]],
    dict[str, HofOrcaCalculation],
    dict[str, Mapping[str, object]],
]:
    igmh = configuration.igmh
    assert igmh is not None
    system = configuration.system
    provenance = _provenance(configuration)
    density_protocol = {
        **igmh.density_metadata,
        "adapter": "hof",
        "system_identity": system.system_identity,
        "density_required": True,
        "geometry_source": igmh.geometry_source,
    }
    density_keywords = " ".join(
        filter(None, (igmh.method, igmh.basis, "TightSCF" if igmh.tight_scf else ""))
    )
    calculation = _orca_plan(
        configuration,
        node_id="igmh_density",
        role="igmh_density_source",
        stage_type=StageType.SP,
        keywords=density_keywords,
        protocol=density_protocol,
    )
    density = DensityArtifact(
        producing_calculation=calculation.calculation_id,
        method=igmh.method,
        basis=igmh.basis,
        protocol=density_protocol,
        parent_artifacts=(optimized_structure.artifact_id,),
        files={"wavefunction": "igmh/density_source.gbw"},
        validation=PLANNED,
        provenance=provenance,
        metadata={"system_id": system.system_id, "density_role": "igmh_source"},
    )
    density_graph = WorkflowGraph(
        f"hof_density_{_node_token(system.system_id)}",
        (
            CalculationNode(
                "igmh_density",
                operation="orca_single_point",
                role="density_generation",
                requires=(ArtifactRequirement("StructureArtifact"),),
                produces=("DensityArtifact",),
                configuration={"calculation_id": calculation.calculation_id, **density_protocol},
            ),
        ),
        external_inputs=("StructureArtifact",),
        provenance={"adapter_branch": "density"},
    )

    outputs = default_igmh_outputs()
    fragment_values = [
        [index + 1 for index in fragment.atom_indices] for fragment in system.fragments
    ]
    analysis_configuration: dict[str, object] = {
        "operation": AnalysisOperation.IGMH.value,
        "profile": igmh.profile,
        "cube_generation": igmh.cube_generation,
        "grid_spacing_bohr": igmh.grid_spacing_bohr,
        "grid_spacing_required_at_execution": igmh.grid_spacing_bohr is None,
        "fragments": {
            "fragment_a": fragment_values[0],
            "fragment_b": fragment_values[1],
            "indexing": "one_based",
            "require_complete_partition": True,
            "allow_overlap": False,
        },
        "outputs": [item.to_dict() for item in outputs],
        "visualization": dict(igmh.visualization),
        "execute": False,
    }
    generic_graph = analysis_workflow_graph(
        AnalysisOperation.IGMH, analysis_configuration
    )
    generic_node = generic_graph.node_map["multiwfn_igmh"]
    analysis_graph = WorkflowGraph(
        f"hof_{generic_graph.graph_id}",
        (
            replace(
                generic_node,
                dependencies=(),
                requires=(ArtifactRequirement("DensityArtifact"),),
            ),
        ),
        external_inputs=("DensityArtifact",),
        provenance={
            "adapter_branch": "igmh",
            "generic_workflow": generic_graph.graph_id,
        },
    )
    igmh_artifact = IGMHArtifact(
        producing_calculation=stable_hash(
            {
                "adapter": "multiwfn",
                "operation": "IGMH",
                "parent": density.artifact_id,
                "protocol": analysis_configuration,
            }
        ),
        method=igmh.method,
        basis=igmh.basis,
        protocol=analysis_configuration,
        parent_artifacts=(density.artifact_id,),
        files={item.role: f"igmh/{item.output_path}" for item in outputs},
        validation=PLANNED,
        provenance={
            **provenance,
            "generating_program": "Multiwfn",
            "multiwfn_version": None,
        },
        metadata={
            "system_id": system.system_id,
            "analysis": "IGMH",
            "density_source": density.artifact_id,
            "grid_spacing_bohr": igmh.grid_spacing_bohr,
            "multiwfn_protocol": analysis_configuration,
            "visualization": dict(igmh.visualization),
        },
    )
    return (
        density_graph,
        analysis_graph,
        {"igmh_density": (density,), "multiwfn_igmh": (igmh_artifact,)},
        {"igmh_density": calculation},
        {"multiwfn_igmh": analysis_configuration},
    )


def _validate_complete_plan(plan: HofWorkflowPlan) -> None:
    node_map = plan.graph.node_map
    if plan.graph.external_inputs:
        raise ValueError("complete HOF workflow contains unresolved external inputs")
    if set(plan.artifact_templates) != set(node_map):
        missing = sorted(set(node_map) - set(plan.artifact_templates))
        extra = sorted(set(plan.artifact_templates) - set(node_map))
        raise ValueError(
            f"HOF artifact templates do not match workflow nodes; missing={missing}, extra={extra}"
        )
    artifact_nodes: dict[str, str] = {}
    artifacts: list[Artifact] = []
    for node_id, node_artifacts in plan.artifact_templates.items():
        declared = node_map[node_id].produces
        for artifact in node_artifacts:
            if not any(artifact_matches_type(artifact, item) for item in declared):
                raise ValueError(
                    f"artifact {artifact.artifact_type} is not declared by node {node_id}"
                )
            if artifact.artifact_id in artifact_nodes:
                raise ValueError("HOF plan contains duplicate artifact identities")
            artifact_nodes[artifact.artifact_id] = node_id
            artifacts.append(artifact)

    ancestors: dict[str, set[str]] = {}
    for node_id in plan.graph.topological_order():
        node = node_map[node_id]
        ancestors[node_id] = set(node.dependencies).union(
            *(ancestors[dependency] for dependency in node.dependencies)
        ) if node.dependencies else set()
    referenced: set[str] = set()
    for artifact in artifacts:
        node_id = artifact_nodes[artifact.artifact_id]
        if node_id != "input_structure" and not artifact.parent_artifacts:
            raise ValueError(f"planned artifact {artifact.artifact_type} has no parents")
        for parent_id in artifact.parent_artifacts:
            parent_node = artifact_nodes.get(parent_id)
            if parent_node is None:
                raise ValueError(f"planned artifact has unknown parent {parent_id}")
            if parent_node != node_id and parent_node not in ancestors[node_id]:
                raise ValueError(
                    f"artifact lineage parent from {parent_node} is not upstream of {node_id}"
                )
            referenced.add(parent_id)

    terminal_types = {"LEDArtifact", "DeformationEnergyArtifact", "IGMHArtifact"}
    orphans = sorted(
        artifact.artifact_type
        for artifact in artifacts
        if artifact.artifact_id not in referenced
        and artifact.artifact_type not in terminal_types
    )
    if orphans:
        raise ValueError("HOF plan contains orphan artifacts: " + ", ".join(orphans))

    for node in plan.graph.nodes:
        if node.operation and node.operation.startswith("orca_"):
            if node.node_id not in plan.orca_calculations:
                raise ValueError(f"ORCA workflow node lacks a plan: {node.node_id}")
        if node.operation == "multiwfn_analysis" and node.node_id not in plan.multiwfn_plans:
            raise ValueError(f"Multiwfn workflow node lacks a plan: {node.node_id}")


def build_hof_workflow_plan(configuration: HofAdapterConfiguration) -> HofWorkflowPlan:
    """Build and validate the complete HOF workflow without launching programs."""

    _require_complete_configuration(configuration)
    geometry_graph, geometry_artifacts, geometry_plans, optimized = _geometry_branch(
        configuration
    )
    interaction = build_hof_interaction_workflow(
        configuration, structure_artifact=optimized
    )
    deformation_graph, deformation_artifacts, deformation_plans = _deformation_branch(
        configuration, optimized
    )
    (
        density_graph,
        analysis_graph,
        analysis_artifacts,
        density_plans,
        multiwfn_plans,
    ) = _density_igmh_branch(configuration, optimized)

    structure_consumers = ["dimer", "igmh_density"]
    for fragment in configuration.system.fragments:
        token = _node_token(fragment.fragment_id)
        structure_consumers.extend(
            (f"distorted_fragment_{token}_energy", f"relax_fragment_{token}")
        )
    bindings = [
        ArtifactBinding("geometry_optimization", node_id, "StructureArtifact")
        for node_id in structure_consumers
    ]
    bindings.append(
        ArtifactBinding("igmh_density", "multiwfn_igmh", "DensityArtifact")
    )
    graph = WorkflowGraph.compose(
        f"hof_complete_{_node_token(configuration.system.system_id)}",
        (
            geometry_graph,
            interaction.graph,
            deformation_graph,
            density_graph,
            analysis_graph,
        ),
        bindings=bindings,
        provenance={
            "adapter": "cmw.adapters.hof",
            "system_identity": configuration.system.system_identity,
            "execution": "planned_only",
        },
    )
    artifacts = {
        **geometry_artifacts,
        **dict(interaction.artifact_templates),
        **deformation_artifacts,
        **analysis_artifacts,
    }
    orca_plans = {
        **geometry_plans,
        **dict(interaction.orca_calculations),
        **deformation_plans,
        **density_plans,
    }
    return HofWorkflowPlan(
        configuration=configuration,
        graph=graph,
        artifact_templates=artifacts,
        orca_calculations=orca_plans,
        multiwfn_plans=multiwfn_plans,
        metadata={
            "execution": "planned_only",
            "branches": ["geometry", "interaction", "deformation", "density", "igmh"],
            "synthetic_results": False,
        },
    )


__all__ = ["HofWorkflowPlan", "build_hof_workflow_plan"]
