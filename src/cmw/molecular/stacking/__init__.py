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
    ExcitedStateRecord,
    NTOOrbitalRecord,
    create_excited_state_artifact,
    create_nto_artifact,
    planned_excited_state_artifacts,
    validate_excited_state_artifact,
    validate_nto_artifact,
)
from .hole_electron import (
    DeferredStateSelectionError,
    FragmentDefinition,
    HoleElectronAnalysisPlan,
    HoleElectronContractError,
    HoleElectronMetrics,
    HoleElectronProtocol,
    build_hole_electron_command_spec,
    create_hole_electron_artifact,
    plan_hole_electron_analysis,
    validate_hole_electron_artifact,
)
from .materialization import stacking_execution_plan
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
    "DeferredStateSelectionError",
    "ExcitedStateArtifact",
    "ExcitedStateContractError",
    "ExcitedStateProtocol",
    "ExcitedStateRecord",
    "FixedRegion",
    "FragmentDefinition",
    "GroundStateProtocol",
    "HoleElectronAnalysisPlan",
    "HoleElectronArtifact",
    "HoleElectronContractError",
    "HoleElectronMetrics",
    "HoleElectronProtocol",
    "NTOArtifact",
    "NTOOrbitalRecord",
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
    "create_excited_state_artifact",
    "create_nto_artifact",
    "extract_stacking_template",
    "plan_hole_electron_analysis",
    "planned_excited_state_artifacts",
    "prepare_vertical_stacking_workflow",
    "stacking_execution_plan",
    "render_orca_fixed_region_block",
    "validate_dimer_structure",
    "validate_excited_state_artifact",
    "validate_hole_electron_artifact",
    "validate_nto_artifact",
    "validate_relaxation_protocol",
    "validate_stacking_artifact_lineage",
    "validate_stacking_template",
]
