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
    prepare_hof_orca_geometry_input,
    render_hof_orca_input,
)
from .validation import HofValidationError, validate_hof_system
from .workflow import HofInteractionPlan, build_hof_interaction_workflow
from .planner import HofWorkflowPlan, build_hof_workflow_plan
from .execution import (
    materialize_hof_orca_node,
    materialize_relaxed_fragment_energy,
    promote_hof_geometry,
)
from .command_queue import (
    authorize_hof_command_queue,
    generate_hof_command_queue,
    verify_hof_command_queue,
)
from .full_chain import generate_hof_full_chain
from .batch_queue import (
    authorize_hof_batch_queue,
    generate_hof_batch_queue,
    verify_hof_batch_queue,
)

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
    "authorize_hof_command_queue",
    "authorize_hof_batch_queue",
    "build_hof_interaction_workflow",
    "build_hof_orca_calculations",
    "build_hof_workflow_plan",
    "configuration_from_documents",
    "generate_hof_command_queue",
    "generate_hof_full_chain",
    "generate_hof_batch_queue",
    "load_hof_configuration",
    "load_yaml_document",
    "materialize_relaxed_fragment_energy",
    "materialize_hof_orca_node",
    "prepare_hof_orca_geometry_input",
    "promote_hof_geometry",
    "render_hof_orca_input",
    "validate_hof_system",
    "verify_hof_command_queue",
    "verify_hof_batch_queue",
]
