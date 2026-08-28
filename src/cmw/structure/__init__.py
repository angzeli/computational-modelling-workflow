"""Minimal structure contracts for reusable computational workflows."""

from typing import TYPE_CHECKING, Any

from .xyz import Atom, XYZGeometry, geometry_hash, read_xyz, write_xyz

if TYPE_CHECKING:
    from .conversion import inspect_xyz_to_poscar, xyz_to_poscar


def __getattr__(name: str) -> Any:
    """Load ASE-backed conversion APIs only when they are requested."""

    if name in {"inspect_xyz_to_poscar", "xyz_to_poscar"}:
        from . import conversion

        return getattr(conversion, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Atom",
    "XYZGeometry",
    "geometry_hash",
    "inspect_xyz_to_poscar",
    "read_xyz",
    "write_xyz",
    "xyz_to_poscar",
]
