"""Execution-plan bindings for reusable vertical-stacking workflows."""

from __future__ import annotations

from pathlib import Path

from cmw.core.plan_materialization import ExecutionPlan
from cmw.molecular.orca.renderer import orca_execution_plan_node

from .workflow import VerticalStackingWorkflowPlan


def stacking_execution_plan(plan: VerticalStackingWorkflowPlan) -> ExecutionPlan:
    """Bind a scientific stacking plan to CMW's generic ORCA lifecycle.

    Only scientific artifacts that have already passed validation are exposed as
    available inputs.  Downstream geometries therefore remain unresolved until
    the preceding optimization attempt promotes a validated StructureArtifact.
    """

    executable_nodes = []
    for node_id, orca_plan in plan.orca_plans.items():
        outputs = {
            role: Path(path).name for role, path in orca_plan.outputs.items()
        }
        executable_nodes.append(
            orca_execution_plan_node(
                node_id,
                orca_plan.spec,
                planned_artifacts=plan.artifact_templates.get(node_id, ()),
                geometry_artifact_id=(
                    orca_plan.geometry_artifact.artifact_id
                    if orca_plan.geometry_artifact.validation.passed
                    else None
                ),
                outputs=outputs,
            )
        )

    available = {
        node_id: tuple(
            artifact for artifact in artifacts if artifact.validation.passed
        )
        for node_id, artifacts in plan.artifact_templates.items()
    }
    available = {
        node_id: artifacts for node_id, artifacts in available.items() if artifacts
    }
    return ExecutionPlan(
        workflow_graph=plan.graph,
        nodes=tuple(executable_nodes),
        available_artifacts=available,
        provenance={
            "adapter": "cmw.molecular.stacking",
            "scientific_plan_graph_id": plan.graph.graph_id,
            "deferred_nodes": [
                "natural_transition_orbitals",
                "hole_electron_analysis",
            ],
        },
    )


__all__ = ["stacking_execution_plan"]
