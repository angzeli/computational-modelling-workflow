"""Hydrogen-bonded HOF adapter built on generic CMW workflow contracts."""

from .config import (
    configuration_from_documents,
    load_hof_configuration,
    load_yaml_document,
)
from .models import (
    HOF_ADAPTER_SCHEMA_VERSION,
    HofAdapterConfiguration,
    HofFragment,
    HofGeometryProtocol,
    HofHydrogenBond,
    HofIgmhProtocol,
    HofInteractionProtocol,
    HofStartingGeometryProtocol,
    HofSystem,
)
from .orca import (
    HofOrcaCalculation,
    build_hof_orca_calculations,
    render_hof_orca_input,
)
from .validation import HofValidationError, validate_hof_system
from .workflow import HofInteractionPlan, build_hof_interaction_workflow
from .planner import HofWorkflowPlan, build_hof_workflow_plan

__all__ = [
    "HOF_ADAPTER_SCHEMA_VERSION",
    "HofAdapterConfiguration",
    "HofFragment",
    "HofGeometryProtocol",
    "HofHydrogenBond",
    "HofIgmhProtocol",
    "HofInteractionPlan",
    "HofInteractionProtocol",
    "HofOrcaCalculation",
    "HofStartingGeometryProtocol",
    "HofSystem",
    "HofValidationError",
    "HofWorkflowPlan",
    "build_hof_interaction_workflow",
    "build_hof_orca_calculations",
    "build_hof_workflow_plan",
    "configuration_from_documents",
    "load_hof_configuration",
    "load_yaml_document",
    "render_hof_orca_input",
    "validate_hof_system",
]
