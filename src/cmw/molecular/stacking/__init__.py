"""Reusable periodic-registry-informed vertical molecular stacking workflows."""

from .artifacts import (
    ConstrainedOptimizationArtifact,
    DimerStructureArtifact,
    ExcitedStateArtifact,
    HoleElectronArtifact,
    NTOArtifact,
    StackingTemplateArtifact,
)
from .assembly import (
    StackingAssemblyError,
    assemble_vertical_dimer,
    validate_dimer_structure,
)
from .constraints import (
    RelaxationContractError,
    build_relaxation_stage_specs,
    render_orca_fixed_region_block,
    validate_relaxation_protocol,
)
from .excited_state import (
    ExcitedStateContractError,
    ExcitedStateProtocol,
    planned_excited_state_artifacts,
    validate_excited_state_artifact,
)
from .hole_electron import (
    HoleElectronAnalysisPlan,
    HoleElectronContractError,
    HoleElectronMetrics,
    HoleElectronProtocol,
    build_hole_electron_command_spec,
    create_hole_electron_artifact,
    plan_hole_electron_analysis,
)
from .models import (
    FixedRegion,
    GroundStateProtocol,
    PeriodicPairDefinition,
    RelaxationProtocol,
    StackingGeometry,
)
from .registry import (
    StackingRegistryError,
    extract_stacking_template,
    validate_stacking_template,
)
from .validation import (
    StackingWorkflowValidationError,
    validate_stacking_artifact_lineage,
)
from .workflow import (
    StackingOrcaPlan,
    VerticalStackingProtocol,
    VerticalStackingWorkflowPlan,
    build_vertical_stacking_workflow,
    prepare_vertical_stacking_workflow,
)

__all__ = [
    "ConstrainedOptimizationArtifact",
    "DimerStructureArtifact",
    "ExcitedStateArtifact",
    "ExcitedStateContractError",
    "ExcitedStateProtocol",
    "FixedRegion",
    "GroundStateProtocol",
    "HoleElectronAnalysisPlan",
    "HoleElectronArtifact",
    "HoleElectronContractError",
    "HoleElectronMetrics",
    "HoleElectronProtocol",
    "NTOArtifact",
    "PeriodicPairDefinition",
    "RelaxationContractError",
    "RelaxationProtocol",
    "StackingAssemblyError",
    "StackingGeometry",
    "StackingOrcaPlan",
    "StackingRegistryError",
    "StackingTemplateArtifact",
    "StackingWorkflowValidationError",
    "VerticalStackingProtocol",
    "VerticalStackingWorkflowPlan",
    "assemble_vertical_dimer",
    "build_hole_electron_command_spec",
    "build_relaxation_stage_specs",
    "build_vertical_stacking_workflow",
    "create_hole_electron_artifact",
    "extract_stacking_template",
    "plan_hole_electron_analysis",
    "planned_excited_state_artifacts",
    "prepare_vertical_stacking_workflow",
    "render_orca_fixed_region_block",
    "validate_dimer_structure",
    "validate_excited_state_artifact",
    "validate_relaxation_protocol",
    "validate_stacking_artifact_lineage",
    "validate_stacking_template",
]
