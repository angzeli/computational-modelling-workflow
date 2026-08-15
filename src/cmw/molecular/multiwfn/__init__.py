"""Reusable Multiwfn runtime and operation-level automation contracts."""

from .adapter import (
    MultiwfnAdapterError,
    MultiwfnCommandSpec,
    MultiwfnOutputSpec,
    build_command_spec,
    discover_outputs,
)
from .runtime import DEFAULT_MULTIWFN_NTHREADS, MultiwfnRuntime

__all__ = [
    "DEFAULT_MULTIWFN_NTHREADS",
    "MultiwfnAdapterError",
    "MultiwfnCommandSpec",
    "MultiwfnOutputSpec",
    "MultiwfnRuntime",
    "build_command_spec",
    "discover_outputs",
]
