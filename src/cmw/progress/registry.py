"""Explicit lazy internal adapters; no executable plugin discovery."""
from dataclasses import dataclass
from typing import Callable
from .models import CardGroup, ProgressAdapter, ProgressSnapshot, RunContext


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
    card_groups: Callable[[ProgressSnapshot], tuple[CardGroup, ...]] | None = None


def _vasp(context):
    from .vasp import VaspAdapter
    return VaspAdapter(context)


def _vasp_card(snapshot):
    from .vasp_card import vasp_card_groups
    return vasp_card_groups(snapshot)


def _multiwfn(context):
    from .multiwfn import MultiwfnAdapter
    return MultiwfnAdapter(context)


def _orca(context):
    from .orca import OrcaAdapter
    return OrcaAdapter(context)


_REGISTRY = {'VASP': Registration(_vasp, (
    SourcePlan('outcar', 'OUTCAR'), SourcePlan('oszicar', 'OSZICAR', alternatives=('stdout',)),
    SourcePlan('poscar', 'POSCAR', 'inputs', True),
    SourcePlan('incar', 'INCAR', 'inputs', True),
), (SourcePlan('stdout', None, optional=True),), _vasp_card), 'ORCA': Registration(_orca, (
    SourcePlan('orca_output', None, 'orca-output', stages=True),
)), 'MULTIWFN': Registration(_multiwfn, (
    SourcePlan('multiwfn_session', None),
), (SourcePlan('multiwfn_events', None, optional=True),
    SourcePlan('multiwfn_validation', None, optional=True),
    SourcePlan('multiwfn_metadata', None, optional=True)))}


def register_adapter(engine, factory, *, sources=(), source_roles=(), card_groups=None):
    """Register a session-local trusted callable, e.g. a fictional test adapter."""
    _REGISTRY[engine.upper()] = Registration(factory, tuple(sources), tuple(source_roles), card_groups)


def get_registration(engine):
    return _REGISTRY.get(engine.upper())
