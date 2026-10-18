"""Explicit lazy internal adapters; no executable plugin discovery."""
from dataclasses import dataclass
from typing import Callable
from .models import ProgressAdapter, RunContext


@dataclass(frozen=True)
class SourcePlan:
    role: str
    filename: str | None
    location_role: str = 'output'
    optional: bool = False
    stages: bool = False
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class Registration:
    factory: Callable[[RunContext], ProgressAdapter]
    sources: tuple[SourcePlan, ...] = ()
    source_roles: tuple[SourcePlan, ...] = ()


def _vasp(context):
    from .vasp import VaspAdapter
    return VaspAdapter(context)


def _orca(context):
    from .orca import OrcaAdapter
    return OrcaAdapter(context)


_REGISTRY = {'VASP': Registration(_vasp, (
    SourcePlan('outcar', 'OUTCAR'), SourcePlan('oszicar', 'OSZICAR', alternatives=('stdout',)),
    SourcePlan('poscar', 'POSCAR', 'inputs', True),
), (SourcePlan('stdout', None, optional=True),)), 'ORCA': Registration(_orca, (
    SourcePlan('orca_output', None, 'orca-output', stages=True),
))}


def register_adapter(engine, factory, *, sources=(), source_roles=()):
    """Register a session-local trusted callable, e.g. a fictional test adapter."""
    _REGISTRY[engine.upper()] = Registration(factory, tuple(sources), tuple(source_roles))


def get_registration(engine):
    return _REGISTRY.get(engine.upper())
