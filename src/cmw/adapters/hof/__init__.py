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
    HofHydrogenBond,
    HofInteractionProtocol,
    HofSystem,
)
from .orca import (
    HofOrcaCalculation,
    build_hof_orca_calculations,
    render_hof_orca_input,
)
from .validation import HofValidationError, validate_hof_system
from .workflow import HofInteractionPlan, build_hof_interaction_workflow

__all__ = [
    "HOF_ADAPTER_SCHEMA_VERSION",
    "HofAdapterConfiguration",
    "HofFragment",
    "HofHydrogenBond",
    "HofInteractionPlan",
    "HofInteractionProtocol",
    "HofOrcaCalculation",
    "HofSystem",
    "HofValidationError",
    "build_hof_interaction_workflow",
    "build_hof_orca_calculations",
    "configuration_from_documents",
    "load_hof_configuration",
    "load_yaml_document",
    "render_hof_orca_input",
    "validate_hof_system",
]
