"""Reusable molecular workflow plans and state machines."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .opt_freq_sp import (
    CANONICAL_MODES,
    WorkflowConfig,
    legacy_workflow_graph,
    load_workflow_config,
)
if TYPE_CHECKING:
    from .intermolecular_led import (
        INTERMOLECULAR_LED_CONTRACT,
        IntermolecularLEDPlan,
        IntermolecularLEDWorkflowError,
        LEDFragmentElectronicState,
        build_intermolecular_led_plan,
        check_intermolecular_led_reuse,
        finalize_intermolecular_led_artifact,
        fragment_partition_hash,
        led_fragment_reference_identity,
        validate_intermolecular_led_artifact,
    )

_ANALYSIS_EXPORTS = {
    'intermolecular_led': (
        'INTERMOLECULAR_LED_CONTRACT',
        'IntermolecularLEDPlan',
        'IntermolecularLEDWorkflowError',
        'LEDFragmentElectronicState',
        'build_intermolecular_led_plan',
        'check_intermolecular_led_reuse',
        'finalize_intermolecular_led_artifact',
        'fragment_partition_hash',
        'led_fragment_reference_identity',
        'validate_intermolecular_led_artifact',
    ),
}


def __getattr__(name: str) -> Any:
    """Load analysis exports only when callers request them."""
    if name in _ANALYSIS_EXPORTS:
        return import_module(f".{name}", __name__)
    for module, names in _ANALYSIS_EXPORTS.items():
        if name in names:
            value = getattr(import_module(f".{module}", __name__), name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | set(_ANALYSIS_EXPORTS))


__all__ = [
    "CANONICAL_MODES",
    "INTERMOLECULAR_LED_CONTRACT",
    "IntermolecularLEDPlan",
    "IntermolecularLEDWorkflowError",
    "LEDFragmentElectronicState",
    "WorkflowConfig",
    "build_intermolecular_led_plan",
    "check_intermolecular_led_reuse",
    "finalize_intermolecular_led_artifact",
    "fragment_partition_hash",
    "legacy_workflow_graph",
    "led_fragment_reference_identity",
    "load_workflow_config",
    "validate_intermolecular_led_artifact",
]
