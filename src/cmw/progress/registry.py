"""Explicit lazy internal adapters; no executable plugin discovery."""
from dataclasses import dataclass
from typing import Callable
from .models import ProgressAdapter, RunContext


@dataclass(frozen=True)
class SourcePlan:
    role: str
    filename: str
    location_role: str = 'output'
    optional: bool = False


@dataclass(frozen=True)
class Registration:
    factory: Callable[[RunContext], ProgressAdapter]
    sources: tuple[SourcePlan, ...] = ()


def _vasp(context):
    from .vasp import VaspAdapter
    return VaspAdapter(context)


_REGISTRY = {'VASP': Registration(_vasp, (
    SourcePlan('outcar', 'OUTCAR'), SourcePlan('oszicar', 'OSZICAR'),
    SourcePlan('poscar', 'POSCAR', 'inputs', True),
))}


def register_adapter(engine, factory, *, sources=()):
    """Register a session-local trusted callable, e.g. a fictional test adapter."""
    _REGISTRY[engine.upper()] = Registration(factory, tuple(sources))


def get_registration(engine):
    return _REGISTRY.get(engine.upper())
