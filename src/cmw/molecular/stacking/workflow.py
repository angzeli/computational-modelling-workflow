"""Composition of registry, relaxation, excited-state, and analysis branches."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    ArtifactValidation,
    ConstrainedOptimizationArtifact,
    DimerStructureArtifact,
    ExcitedStateArtifact,
    FrequencyArtifact,
    NTOArtifact,
    OptimizationArtifact,
    SinglePointArtifact,
    StackingTemplateArtifact,
    StructureArtifact,
    ValidationStatus,
    WavefunctionArtifact,
)
from cmw.core.execution_layout import ExecutionLayout
from cmw.core.provenance import stable_hash
from cmw.core.workflow_graph import (
    ArtifactRequirement,
    CalculationNode,
    DerivedResultNode,
    WorkflowGraph,
)
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.status import StageType

from .assembly import assemble_vertical_dimer
from .constraints import (
    build_relaxation_stage_specs,
    validate_relaxation_protocol,
)
from .excited_state import ExcitedStateProtocol
from .hole_electron import (
    HoleElectronAnalysisPlan,
    HoleElectronProtocol,
    plan_hole_electron_analysis,
)
from .models import GroundStateProtocol, PeriodicPairDefinition, RelaxationProtocol
from .registry import extract_stacking_template
from .validation import validate_stacking_artifact_lineage


def _planned(reason: str) -> ArtifactValidation:
    return ArtifactValidation(
        ValidationStatus.UNKNOWN,
        {"planned": True, "executed": False},
        "PLANNED_NOT_EXECUTED",
        reason,
    )


@dataclass(frozen=True)
class VerticalStackingProtocol:
    relaxation: RelaxationProtocol
    relaxation_method: GroundStateProtocol
    ground_state: GroundStateProtocol
    excited_state: ExcitedStateProtocol
    hole_electron: HoleElectronProtocol
    frequency_method: GroundStateProtocol | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "relaxation": self.relaxation.to_dict(),
            "relaxation_method": self.relaxation_method.to_dict(),
            "ground_state": self.ground_state.to_dict(),
            "excited_state": self.excited_state.to_dict(),
            "hole_electron": self.hole_electron.to_dict(),
            "frequency_method": (
                self.frequency_method.to_dict()
                if self.frequency_method is not None
                else None
            ),
        }


@dataclass(frozen=True)
class StackingOrcaPlan:
    node_id: str
    geometry_artifact: StructureArtifact
    spec: OrcaStageSpec
    outputs: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", dict(self.outputs))

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "geometry_artifact": self.geometry_artifact.to_dict(),
            "geometry_hash": self.geometry_artifact.geometry_hash,
            "stage": self.spec.stage_type.value,
            "execution_intent": self.spec.execution_intent.to_dict(),
            "scientific_identity": self.spec.scientific_identity(),
            "outputs": dict(self.outputs),
        }


@dataclass(frozen=True)
class VerticalStackingWorkflowPlan:
    graph: WorkflowGraph
    artifact_templates: Mapping[str, tuple[Artifact, ...]]
    orca_plans: Mapping[str, StackingOrcaPlan]
    hole_electron_plan: HoleElectronAnalysisPlan
    execution_layouts: Mapping[str, ExecutionLayout] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_templates",
            {key: tuple(value) for key, value in self.artifact_templates.items()},
        )
        object.__setattr__(self, "orca_plans", dict(self.orca_plans))
        object.__setattr__(self, "execution_layouts", dict(self.execution_layouts))

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow_graph": self.graph.to_dict(),
            "artifact_templates": {
                node: [artifact.to_dict() for artifact in artifacts]
                for node, artifacts in self.artifact_templates.items()
            },
            "orca_plans": {
                node: plan.to_dict() for node, plan in self.orca_plans.items()
            },
            "multiwfn_hole_electron": self.hole_electron_plan.to_dict(),
            "execution_layouts": {
                node: layout.to_dict()
                for node, layout in self.execution_layouts.items()
            },
        }


def _planned_structure(
    node_id: str,
    source: StructureArtifact,
    parents: Sequence[str],
    output_path: str,
) -> StructureArtifact:
    return StructureArtifact(
        producing_calculation=node_id,
        parent_artifacts=tuple(parents),
        files={"structure": output_path},
        validation=_planned("optimized structure has been planned but not generated"),
        provenance={"execution": "planned_only"},
        metadata={
            **dict(source.metadata),
            "source_structure_artifact": source.artifact_id,
        },
        source=node_id,
        format=source.format,
        atom_count=source.atom_count,
        elemental_composition=source.elemental_composition,
        charge=source.charge,
        multiplicity=source.multiplicity,
        geometry_hash="",
    )


def _execution_layouts(
    *,
    project_root: str | Path | None,
    system_identifier: str,
    graph: WorkflowGraph,
    orca_plans: Mapping[str, StackingOrcaPlan],
    hole_plan: HoleElectronAnalysisPlan,
    attempt_identifiers: Mapping[str, str],
) -> dict[str, ExecutionLayout]:
    if project_root is None:
        return {}
    layouts: dict[str, ExecutionLayout] = {}
    identities = {
        node_id: {
            "graph_id": graph.graph_id,
            "node_id": node_id,
            "geometry_artifact": plan.geometry_artifact.artifact_id,
            "scientific_identity": plan.spec.scientific_identity(),
        }
        for node_id, plan in orca_plans.items()
    }
    identities["hole_electron_analysis"] = {
        "graph_id": graph.graph_id,
        "node_id": "hole_electron_analysis",
        "protocol": hole_plan.protocol.to_dict(),
        "parent_artifacts": list(hole_plan.artifact.parent_artifacts),
    }
    for node_id, identity in identities.items():
        layouts[node_id] = ExecutionLayout(
            Path(project_root),
            system_identifier,
            node_id,
            stable_hash(identity),
            attempt_identifiers.get(node_id, "attempt_001"),
        )
    return layouts


def build_vertical_stacking_workflow(
    *,
    periodic_source: StructureArtifact,
    monomer_a: StructureArtifact,
    template: StackingTemplateArtifact,
    dimer: DimerStructureArtifact,
    protocol: VerticalStackingProtocol,
    monomer_b: StructureArtifact | None = None,
    graph_id: str = "vertical_stacking",
    project_root: str | Path | None = None,
    system_identifier: str = "stacking_system",
    attempt_identifiers: Mapping[str, str] | None = None,
) -> VerticalStackingWorkflowPlan:
    """Build a validated, non-executing vertical stacking workflow plan."""

    if dimer.atom_count is None:
        raise ValueError("DimerStructureArtifact requires atom_count for relaxation")
    validate_relaxation_protocol(protocol.relaxation, atom_count=dimer.atom_count)
    relaxation_specs = build_relaxation_stage_specs(
        protocol.relaxation, protocol.relaxation_method
    )
    planned = _planned("calculation has been planned but not executed")

    artifact_templates: dict[str, tuple[Artifact, ...]] = {
        "periodic_source": (periodic_source,),
        "monomer_a": (monomer_a,),
        "stacking_template": (template,),
        "vertical_dimer": (dimer,),
    }
    monomer_dependencies = ["monomer_a"]
    monomer_nodes = [
        CalculationNode("monomer_a", produces=("StructureArtifact",))
    ]
    if monomer_b is not None and monomer_b.artifact_id != monomer_a.artifact_id:
        monomer_dependencies.append("monomer_b")
        monomer_nodes.append(
            CalculationNode("monomer_b", produces=("StructureArtifact",))
        )
        artifact_templates["monomer_b"] = (monomer_b,)

    constrained_artifact = ConstrainedOptimizationArtifact(
        producing_calculation="constrained_optimization",
        method=protocol.relaxation_method.method,
        basis=protocol.relaxation_method.basis,
        protocol={
            **dict(protocol.relaxation_method.protocol),
            "constraints": {
                "indexing": "zero_based",
                "fixed_regions": [
                    region.to_dict() for region in protocol.relaxation.fixed_regions
                ],
                "preserve_stacking_registry": (
                    protocol.relaxation.preserve_stacking_registry
                ),
            },
        },
        parent_artifacts=(dimer.artifact_id,),
        files={"output": "relaxation/constrained/stage.out"},
        validation=planned,
        provenance={"execution": "planned_only"},
    )
    constrained_structure = _planned_structure(
        "constrained_optimization",
        dimer,
        (dimer.artifact_id, constrained_artifact.artifact_id),
        "relaxation/constrained/stage.xyz",
    )
    artifact_templates["constrained_optimization"] = (
        constrained_artifact,
        constrained_structure,
    )
    final_node = "constrained_optimization"
    final_optimization: Artifact = constrained_artifact
    final_structure = constrained_structure
    if protocol.relaxation.full_relaxation:
        full_artifact = OptimizationArtifact(
            producing_calculation="full_optimization",
            method=protocol.relaxation_method.method,
            basis=protocol.relaxation_method.basis,
            protocol={
                **dict(protocol.relaxation_method.protocol),
                "constraints": {"fixed_regions": []},
            },
            parent_artifacts=(
                constrained_artifact.artifact_id,
                constrained_structure.artifact_id,
            ),
            files={"output": "relaxation/full/stage.out"},
            validation=planned,
            provenance={"execution": "planned_only"},
        )
        full_structure = _planned_structure(
            "full_optimization",
            constrained_structure,
            (constrained_structure.artifact_id, full_artifact.artifact_id),
            "relaxation/full/stage.xyz",
        )
        artifact_templates["full_optimization"] = (full_artifact, full_structure)
        final_node = "full_optimization"
        final_optimization = full_artifact
        final_structure = full_structure

    frequency_protocol = protocol.frequency_method or protocol.ground_state
    frequency_artifact: FrequencyArtifact | None = None
    if frequency_protocol.frequency:
        frequency_artifact = FrequencyArtifact(
            producing_calculation="frequency",
            method=frequency_protocol.method,
            basis=frequency_protocol.basis,
            protocol=dict(frequency_protocol.protocol),
            parent_artifacts=(
                final_optimization.artifact_id,
                final_structure.artifact_id,
            ),
            files={"output": "ground_state/frequency.out"},
            validation=planned,
            provenance={"execution": "planned_only"},
        )
        artifact_templates["frequency"] = (frequency_artifact,)

    ground_parents = (
        final_structure.artifact_id,
        final_optimization.artifact_id,
    ) + ((frequency_artifact.artifact_id,) if frequency_artifact else ())
    ground_energy = SinglePointArtifact(
        producing_calculation="ground_state",
        method=protocol.ground_state.method,
        basis=protocol.ground_state.basis,
        protocol=dict(protocol.ground_state.protocol),
        parent_artifacts=ground_parents,
        files={"output": "ground_state/stage.out"},
        validation=planned,
        provenance={"execution": "planned_only"},
    )
    ground_wavefunction = WavefunctionArtifact(
        producing_calculation="ground_state",
        method=protocol.ground_state.method,
        basis=protocol.ground_state.basis,
        protocol=dict(protocol.ground_state.protocol),
        parent_artifacts=ground_parents,
        files={"wavefunction": "ground_state/stage.gbw"},
        validation=planned,
        provenance={"execution": "planned_only"},
    )
    artifact_templates["ground_state"] = (ground_energy, ground_wavefunction)

    excited = ExcitedStateArtifact(
        producing_calculation="excited_state",
        method=protocol.excited_state.method,
        basis=protocol.excited_state.basis,
        protocol=protocol.excited_state.to_dict(),
        parent_artifacts=(final_structure.artifact_id, ground_wavefunction.artifact_id),
        files={
            "output": "excited_state/stage.out",
            "wavefunction": "excited_state/stage.gbw",
        },
        validation=planned,
        provenance={"execution": "planned_only"},
        metadata={
            "geometry_source_artifact": final_structure.artifact_id,
            "source_geometry_hash": final_structure.geometry_hash,
            "planned_source_artifact": final_structure.artifact_id,
            "functional": protocol.excited_state.functional,
            "state_selection": dict(protocol.excited_state.state_selection),
        },
    )
    artifact_templates["excited_state"] = (excited,)
    nto: NTOArtifact | None = None
    if protocol.excited_state.generate_ntos:
        nto = NTOArtifact(
            producing_calculation="natural_transition_orbitals",
            method=protocol.excited_state.method,
            basis=protocol.excited_state.basis,
            protocol={
                "state_selection": dict(protocol.excited_state.state_selection),
                "source_number_of_roots": protocol.excited_state.number_of_roots,
            },
            parent_artifacts=(excited.artifact_id,),
            files={"orbitals": "excited_state/nto.molden.input"},
            validation=planned,
            provenance={"execution": "planned_only"},
            metadata={
                "excited_state_artifact": excited.artifact_id,
                "generation_method": (
                    "ORCA TDDFT/TDA natural transition orbitals"
                ),
                "planned_state_selection": dict(
                    protocol.excited_state.state_selection
                ),
                "visualization": dict(protocol.excited_state.visualization)
            },
        )
        artifact_templates["natural_transition_orbitals"] = (nto,)
    hole_plan = plan_hole_electron_analysis(
        excited, protocol.hole_electron, nto=nto
    )
    artifact_templates["hole_electron_analysis"] = (hole_plan.artifact,)

    nodes = [
        CalculationNode("periodic_source", produces=("StructureArtifact",)),
        *monomer_nodes,
        DerivedResultNode(
            "stacking_template",
            dependencies=("periodic_source",),
            operation="extract_stacking_registry",
            requires=(
                ArtifactRequirement(
                    "StructureArtifact", from_nodes=("periodic_source",)
                ),
            ),
            produces=("StackingTemplateArtifact",),
        ),
        DerivedResultNode(
            "vertical_dimer",
            dependencies=(*monomer_dependencies, "stacking_template"),
            operation="assemble_vertical_dimer",
            requires=(
                ArtifactRequirement(
                    "StructureArtifact",
                    len(monomer_dependencies),
                    tuple(monomer_dependencies),
                ),
                ArtifactRequirement(
                    "StackingTemplateArtifact",
                    from_nodes=("stacking_template",),
                ),
            ),
            produces=("DimerStructureArtifact",),
        ),
        CalculationNode(
            "constrained_optimization",
            dependencies=("vertical_dimer",),
            operation="constraint_aware_optimization",
            requires=(
                ArtifactRequirement(
                    "DimerStructureArtifact", from_nodes=("vertical_dimer",)
                ),
            ),
            produces=("ConstrainedOptimizationArtifact", "StructureArtifact"),
            configuration={
                "execution_intent": relaxation_specs[
                    "constrained_optimization"
                ].execution_intent.to_dict(),
                "constraints": protocol.relaxation.to_dict()["constrained"],
            },
        ),
    ]
    if protocol.relaxation.full_relaxation:
        nodes.append(
            CalculationNode(
                "full_optimization",
                dependencies=("constrained_optimization",),
                operation="full_optimization",
                requires=(
                    ArtifactRequirement(
                        "StructureArtifact",
                        from_nodes=("constrained_optimization",),
                    ),
                    ArtifactRequirement(
                        "ConstrainedOptimizationArtifact",
                        from_nodes=("constrained_optimization",),
                    ),
                ),
                produces=("OptimizationArtifact", "StructureArtifact"),
                configuration={
                    "execution_intent": relaxation_specs[
                        "full_optimization"
                    ].execution_intent.to_dict(),
                    "constraints": {"fixed_regions": []},
                },
            )
        )
    ground_dependencies = [final_node]
    if frequency_artifact is not None:
        nodes.append(
            CalculationNode(
                "frequency",
                dependencies=(final_node,),
                operation="frequency",
                requires=(
                    ArtifactRequirement(
                        "StructureArtifact", from_nodes=(final_node,)
                    ),
                ),
                produces=("FrequencyArtifact",),
            )
        )
        ground_dependencies.append("frequency")
    nodes.extend(
        (
            CalculationNode(
                "ground_state",
                dependencies=tuple(ground_dependencies),
                operation="ground_state_single_point",
                requires=(
                    ArtifactRequirement(
                        "StructureArtifact", from_nodes=(final_node,)
                    ),
                    ArtifactRequirement(
                        "OptimizationArtifact", from_nodes=(final_node,)
                    ),
                ),
                produces=("SinglePointArtifact", "WavefunctionArtifact"),
            ),
            CalculationNode(
                "excited_state",
                dependencies=(final_node, "ground_state"),
                operation="tddft_tda",
                requires=(
                    ArtifactRequirement(
                        "StructureArtifact", from_nodes=(final_node,)
                    ),
                    ArtifactRequirement(
                        "WavefunctionArtifact", from_nodes=("ground_state",)
                    ),
                ),
                produces=("ExcitedStateArtifact",),
                configuration={
                    "execution_intent": protocol.excited_state.orca_stage_spec()
                    .execution_intent.to_dict(),
                    "state_selection": dict(protocol.excited_state.state_selection),
                },
            ),
        )
    )
    hole_dependencies = ["excited_state"]
    if nto is not None:
        nodes.append(
            DerivedResultNode(
                "natural_transition_orbitals",
                dependencies=("excited_state",),
                operation="generate_ntos",
                requires=(
                    ArtifactRequirement(
                        "ExcitedStateArtifact", from_nodes=("excited_state",)
                    ),
                ),
                produces=("NTOArtifact",),
            )
        )
        hole_dependencies.append("natural_transition_orbitals")
    nodes.append(
        DerivedResultNode(
            "hole_electron_analysis",
            dependencies=tuple(hole_dependencies),
            operation="multiwfn_hole_electron",
            requires=(
                ArtifactRequirement(
                    "ExcitedStateArtifact", from_nodes=("excited_state",)
                ),
            )
            + (
                (
                    ArtifactRequirement(
                        "NTOArtifact",
                        from_nodes=("natural_transition_orbitals",),
                    ),
                )
                if nto is not None
                else ()
            ),
            produces=("HoleElectronArtifact",),
            configuration={"multiwfn_protocol": protocol.hole_electron.to_dict()},
        )
    )
    graph = WorkflowGraph(
        graph_id,
        tuple(nodes),
        provenance={
            "builder": "cmw.molecular.stacking",
            "protocol": protocol.to_dict(),
        },
    )

    orca_plans: dict[str, StackingOrcaPlan] = {
        "constrained_optimization": StackingOrcaPlan(
            "constrained_optimization",
            dimer,
            relaxation_specs["constrained_optimization"],
            {
                "output": "relaxation/constrained/stage.out",
                "final_geometry": "relaxation/constrained/stage.xyz",
            },
        )
    }
    if protocol.relaxation.full_relaxation:
        orca_plans["full_optimization"] = StackingOrcaPlan(
            "full_optimization",
            constrained_structure,
            relaxation_specs["full_optimization"],
            {
                "output": "relaxation/full/stage.out",
                "final_geometry": "relaxation/full/stage.xyz",
            },
        )
    if frequency_artifact is not None:
        orca_plans["frequency"] = StackingOrcaPlan(
            "frequency",
            final_structure,
            OrcaStageSpec(
                StageType.FREQ,
                frequency_protocol.keyword_line,
                protocol={
                    **dict(frequency_protocol.protocol),
                    "method": frequency_protocol.method,
                    "basis": frequency_protocol.basis,
                },
            ),
            {"output": "ground_state/frequency.out"},
        )
    orca_plans["ground_state"] = StackingOrcaPlan(
        "ground_state",
        final_structure,
        OrcaStageSpec(
            StageType.SP,
            protocol.ground_state.keyword_line,
            protocol={
                **dict(protocol.ground_state.protocol),
                "method": protocol.ground_state.method,
                "basis": protocol.ground_state.basis,
            },
        ),
        {
            "output": "ground_state/stage.out",
            "wavefunction": "ground_state/stage.gbw",
        },
    )
    orca_plans["excited_state"] = StackingOrcaPlan(
        "excited_state",
        final_structure,
        protocol.excited_state.orca_stage_spec(),
        {
            "output": "excited_state/stage.out",
            "wavefunction": "excited_state/stage.gbw",
        },
    )
    validate_stacking_artifact_lineage(graph, artifact_templates)
    layouts = _execution_layouts(
        project_root=project_root,
        system_identifier=system_identifier,
        graph=graph,
        orca_plans=orca_plans,
        hole_plan=hole_plan,
        attempt_identifiers=dict(attempt_identifiers or {}),
    )
    return VerticalStackingWorkflowPlan(
        graph, artifact_templates, orca_plans, hole_plan, layouts
    )


def prepare_vertical_stacking_workflow(
    *,
    periodic_source: StructureArtifact,
    pair: PeriodicPairDefinition,
    monomer_a: StructureArtifact,
    dimer_output_path: str | Path,
    protocol: VerticalStackingProtocol,
    extraction_method: str,
    monomer_b: StructureArtifact | None = None,
    charge: int | None = None,
    multiplicity: int | None = None,
    graph_id: str = "vertical_stacking",
    project_root: str | Path | None = None,
    system_identifier: str = "stacking_system",
) -> VerticalStackingWorkflowPlan:
    """Extract, assemble, and plan the complete chain without external execution."""

    template = extract_stacking_template(
        periodic_source, pair, extraction_method=extraction_method
    )
    dimer = assemble_vertical_dimer(
        monomer_a,
        template,
        dimer_output_path,
        monomer_b=monomer_b,
        charge=charge,
        multiplicity=multiplicity,
    )
    return build_vertical_stacking_workflow(
        periodic_source=periodic_source,
        monomer_a=monomer_a,
        monomer_b=monomer_b,
        template=template,
        dimer=dimer,
        protocol=protocol,
        graph_id=graph_id,
        project_root=project_root,
        system_identifier=system_identifier,
    )


__all__ = [
    "StackingOrcaPlan",
    "VerticalStackingProtocol",
    "VerticalStackingWorkflowPlan",
    "build_vertical_stacking_workflow",
    "prepare_vertical_stacking_workflow",
]
