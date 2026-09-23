"""Charge-aware ASE conversion of molecular XYZ files to VASP POSCAR."""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, cast

import ase
from ase import Atoms
from ase.io import read as ase_read
from ase.io import write as ase_write

from cmw.core.provenance import atomic_write_json, file_hash

from .ordering import group_species


RequestedCellMode = Literal["auto", "orthorhombic", "cubic"]
ResolvedCellMode = Literal["orthorhombic", "cubic"]
CELL_MODES: tuple[RequestedCellMode, ...] = ("auto", "orthorhombic", "cubic")
DEFAULT_VACUUM_ANGSTROM = 10.0
VALIDATION_TOLERANCE_ANGSTROM = 1.0e-8
CONVERSION_SCHEMA_VERSION = 1
MAPPING_FILENAME = "atom_mapping.json"
METADATA_FILENAME = "conversion_metadata.json"


@dataclass(frozen=True)
class _ConversionPlan:
    """Validated in-memory conversion state; all lengths are in ångström."""

    input_path: Path
    input_sha256: str
    input_size_bytes: int
    atoms: Atoms
    original_symbols: tuple[str, ...]
    species_order: tuple[str, ...]
    poscar_to_original: tuple[int, ...]
    vacuum: float
    net_charge: int
    requested_cell_mode: RequestedCellMode
    resolved_cell_mode: ResolvedCellMode
    cell_selection_reason: str
    warnings: tuple[str, ...]
    bounding_minimum: tuple[float, float, float]
    bounding_maximum: tuple[float, float, float]
    molecular_spans: tuple[float, float, float]
    cell_lengths: tuple[float, float, float]

    @property
    def atom_reordering_performed(self) -> bool:
        return self.poscar_to_original != tuple(range(len(self.atoms)))

    @property
    def composition(self) -> dict[str, int]:
        counts = Counter(self.original_symbols)
        return {element: counts[element] for element in self.species_order}


def _coordinate_bounds(
    atoms: Atoms,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    """Return Cartesian minima, maxima, and spans in ångström."""

    positions = atoms.get_positions()
    minimum = tuple(float(value) for value in positions.min(axis=0))
    maximum = tuple(float(value) for value in positions.max(axis=0))
    spans = tuple(upper - lower for lower, upper in zip(minimum, maximum, strict=True))
    return minimum, maximum, spans


def _read_single_xyz(path: Path, *, ordinary_xyz_only: bool = False) -> tuple[Atoms, str, int]:
    """Read exactly one molecular XYZ record with ASE and stable input evidence."""

    if not path.exists():
        raise FileNotFoundError(f"XYZ input does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"XYZ input is not a regular file: {path}")

    source_content = path.read_bytes()
    initial_sha256 = hashlib.sha256(source_content).hexdigest()
    source_text = source_content.decode("utf-8")
    if ordinary_xyz_only:
        if path.suffix.casefold() != ".xyz":
            raise ValueError("molecular embedding requires ordinary .xyz input; CIF/extxyz periodic import is unsupported")
        lines = source_text.splitlines()
        comment = lines[1] if len(lines) > 1 else ""
        if re.search(r"\b(?:lattice|pbc|properties|occupancy|occupancies|spacegroup|cell)\s*=", comment, re.I):
            raise ValueError("extended XYZ cell/PBC/property metadata is unsupported by molecular embedding")
        if any(len(line.split()) != 4 for line in lines[2:] if line.strip()):
            raise ValueError("ordinary molecular XYZ requires element and three Cartesian coordinates per atom")
    try:
        records = ase_read(io.StringIO(source_text), format="xyz", index=":")
    except (IndexError, KeyError, OSError, ValueError) as exc:
        raise ValueError(f"ASE could not read XYZ input {path}: {exc}") from exc
    final_sha256 = file_hash(path)
    if final_sha256 != initial_sha256:
        raise ValueError(f"XYZ input changed while it was being read: {path}")
    if not isinstance(records, list):
        records = [records]
    if len(records) != 1:
        raise ValueError(
            f"XYZ input must contain exactly one structure; found {len(records)}: {path}"
        )

    atoms = records[0]
    if len(atoms) < 1:
        raise ValueError("XYZ structure must contain at least one atom")
    if any(number <= 0 for number in atoms.get_atomic_numbers()):
        raise ValueError("XYZ structure contains an unrecognized element")
    if not all(
        math.isfinite(float(value)) for position in atoms.get_positions() for value in position
    ):
        raise ValueError("XYZ coordinates must be finite")
    return atoms, initial_sha256, len(source_content)


def _resolve_cell_mode(
    requested_cell_mode: str, net_charge: int
) -> tuple[RequestedCellMode, ResolvedCellMode, str, tuple[str, ...]]:
    """Resolve the requested cell policy from an explicit formal net charge."""

    if requested_cell_mode not in CELL_MODES:
        raise ValueError(
            f"unsupported cell mode {requested_cell_mode!r}; "
            f"choose one of {', '.join(CELL_MODES)}"
        )
    requested = cast(RequestedCellMode, requested_cell_mode)
    if requested == "auto":
        if net_charge == 0:
            return requested, "orthorhombic", "neutral molecule", ()
        return requested, "cubic", "nonzero net charge", ()

    if requested == "orthorhombic" and net_charge != 0:
        formatted_charge = f"{net_charge:+d}"
        warning = (
            f"Charged structure (net charge {formatted_charge}) explicitly requested an "
            "orthorhombic periodic cell. A non-cubic cell can be problematic for some "
            "VASP isolated-charge electrostatic correction workflows. Cubic is the "
            "recommended CMW default for charged isolated molecules; this conversion "
            "honors the explicit override."
        )
        return (
            requested,
            "orthorhombic",
            "explicit orthorhombic override for charged molecule",
            (warning,),
        )
    if requested == "orthorhombic":
        return requested, "orthorhombic", "explicit orthorhombic selection", ()
    return requested, "cubic", "explicit cubic selection", ()


def _build_plan(
    input_xyz: str | Path,
    *,
    vacuum: float,
    cell_mode: str,
    net_charge: int,
    species_order: Iterable[str] | None,
    ordinary_xyz_only: bool = False,
) -> _ConversionPlan:
    """Validate inputs and construct the centered, species-grouped ASE structure."""

    if isinstance(vacuum, bool):
        raise ValueError("vacuum must be a finite positive length in ångström")
    try:
        selected_vacuum = float(vacuum)
    except (TypeError, ValueError) as exc:
        raise ValueError("vacuum must be a finite positive length in ångström") from exc
    if not math.isfinite(selected_vacuum) or selected_vacuum <= 0.0:
        raise ValueError("vacuum must be a finite positive length in ångström")
    if isinstance(net_charge, bool) or not isinstance(net_charge, int):
        raise ValueError("net charge must be an explicitly supplied integer")
    requested_mode, resolved_mode, selection_reason, warnings = _resolve_cell_mode(
        cell_mode, net_charge
    )

    input_path = Path(input_xyz).expanduser()
    source_atoms, input_sha256, input_size_bytes = _read_single_xyz(
        input_path, ordinary_xyz_only=ordinary_xyz_only
    )
    original_symbols = tuple(source_atoms.get_chemical_symbols())
    selected_species, poscar_to_original = group_species(original_symbols, species_order)

    minimum, maximum, spans = _coordinate_bounds(source_atoms)
    if resolved_mode == "orthorhombic":
        cell_lengths = tuple(span + 2.0 * selected_vacuum for span in spans)
    else:
        cubic_length = max(spans) + 2.0 * selected_vacuum
        cell_lengths = (cubic_length, cubic_length, cubic_length)

    prepared = source_atoms[list(poscar_to_original)]
    prepared_minimum, _, prepared_spans = _coordinate_bounds(prepared)
    shifts = tuple(
        (length - span) / 2.0 - lower
        for length, span, lower in zip(
            cell_lengths, prepared_spans, prepared_minimum, strict=True
        )
    )
    prepared.set_positions(prepared.get_positions() + shifts)
    prepared.set_cell(cell_lengths)
    prepared.set_pbc((True, True, True))

    return _ConversionPlan(
        input_path=input_path,
        input_sha256=input_sha256,
        input_size_bytes=input_size_bytes,
        atoms=prepared,
        original_symbols=original_symbols,
        species_order=selected_species,
        poscar_to_original=poscar_to_original,
        vacuum=selected_vacuum,
        net_charge=net_charge,
        requested_cell_mode=requested_mode,
        resolved_cell_mode=resolved_mode,
        cell_selection_reason=selection_reason,
        warnings=warnings,
        bounding_minimum=minimum,
        bounding_maximum=maximum,
        molecular_spans=spans,
        cell_lengths=cell_lengths,
    )


def inspect_xyz_to_poscar(
    input_xyz: str | Path,
    *,
    vacuum: float = DEFAULT_VACUUM_ANGSTROM,
    cell_mode: str = "auto",
    net_charge: int = 0,
    species_order: Iterable[str] | None = None,
) -> dict[str, object]:
    """Return a charge-aware conversion preview without writing any files."""

    plan = _build_plan(
        input_xyz,
        vacuum=vacuum,
        cell_mode=cell_mode,
        net_charge=net_charge,
        species_order=species_order,
    )
    return {
        "atom_count": len(plan.atoms),
        "composition": plan.composition,
        "molecular_dimensions_angstrom": list(plan.molecular_spans),
        "proposed_cell_dimensions_angstrom": list(plan.cell_lengths),
        "species_order": list(plan.species_order),
        "atom_reordering_performed": plan.atom_reordering_performed,
        "net_charge": plan.net_charge,
        "requested_cell_mode": plan.requested_cell_mode,
        "resolved_cell_mode": plan.resolved_cell_mode,
        "cell_selection_reason": plan.cell_selection_reason,
        "warnings": list(plan.warnings),
        "vacuum_requested_angstrom": plan.vacuum,
        "cell_mode": plan.resolved_cell_mode,
        "periodic_boundary_conditions": [True, True, True],
    }


def _mapping_record(plan: _ConversionPlan) -> dict[str, object]:
    """Build explicit zero-based internal and one-based human atom mappings."""

    original_to_poscar = {
        original_index: poscar_index
        for poscar_index, original_index in enumerate(plan.poscar_to_original)
    }
    return {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "internal_indexing": "zero_based",
        "human_atom_numbering": "one_based",
        "species_order": list(plan.species_order),
        "original_to_poscar": {
            str(index): original_to_poscar[index]
            for index in range(len(plan.original_symbols))
        },
        "poscar_to_original": {
            str(index): original_index
            for index, original_index in enumerate(plan.poscar_to_original)
        },
        "atoms": [
            {
                "element": plan.original_symbols[original_index],
                "original_xyz_zero_based_index": original_index,
                "poscar_zero_based_index": poscar_index,
                "original_xyz_one_based_atom_number": original_index + 1,
                "poscar_one_based_atom_number": poscar_index + 1,
            }
            for poscar_index, original_index in enumerate(plan.poscar_to_original)
        ],
    }


def _vacuum_clearances(atoms: Atoms) -> dict[str, float]:
    """Measure distances from the molecular bounds to all six orthogonal cell faces."""

    minimum, maximum, _ = _coordinate_bounds(atoms)
    cell_lengths = tuple(float(value) for value in atoms.cell.lengths())
    return {
        "x_minus_vacuum": minimum[0],
        "x_plus_vacuum": cell_lengths[0] - maximum[0],
        "y_minus_vacuum": minimum[1],
        "y_plus_vacuum": cell_lengths[1] - maximum[1],
        "z_minus_vacuum": minimum[2],
        "z_plus_vacuum": cell_lengths[2] - maximum[2],
    }


def _validate_written_poscar(
    path: Path | io.StringIO, plan: _ConversionPlan, mapping: dict[str, object]
) -> tuple[Atoms, dict[str, float]]:
    """Read back the POSCAR with ASE and fail on any structural disagreement."""

    try:
        written = ase_read(path, format="vasp")
    except (IndexError, KeyError, OSError, ValueError) as exc:
        raise ValueError(f"ASE could not read the generated POSCAR: {exc}") from exc

    expected_symbols = tuple(plan.atoms.get_chemical_symbols())
    written_symbols = tuple(written.get_chemical_symbols())
    if len(written) != len(plan.atoms):
        raise ValueError("generated POSCAR atom count differs from the XYZ")
    if Counter(written_symbols) != Counter(plan.original_symbols):
        raise ValueError("generated POSCAR composition differs from the XYZ")
    if written_symbols != expected_symbols:
        raise ValueError(
            "generated POSCAR species declarations disagree with coordinate ordering"
        )
    if not all(bool(value) for value in written.get_pbc()):
        raise ValueError("generated POSCAR is not periodic in all three directions")

    for expected, actual in zip(
        plan.atoms.get_positions(), written.get_positions(), strict=True
    ):
        if any(
            abs(float(left) - float(right)) > VALIDATION_TOLERANCE_ANGSTROM
            for left, right in zip(expected, actual, strict=True)
        ):
            raise ValueError("generated POSCAR Cartesian coordinates changed on read-back")

    written_cell = written.cell.array
    for row_index in range(3):
        for column_index in range(3):
            expected = plan.cell_lengths[row_index] if row_index == column_index else 0.0
            if (
                abs(float(written_cell[row_index, column_index]) - expected)
                > VALIDATION_TOLERANCE_ANGSTROM
            ):
                raise ValueError("generated POSCAR cell vectors differ from the planned cell")

    minimum, maximum, _ = _coordinate_bounds(written)
    for lower, upper, length in zip(minimum, maximum, plan.cell_lengths, strict=True):
        if abs((lower + upper) / 2.0 - length / 2.0) > VALIDATION_TOLERANCE_ANGSTROM:
            raise ValueError("generated POSCAR molecule is not centered in the cell")

    clearances = _vacuum_clearances(written)
    if any(
        clearance < plan.vacuum - VALIDATION_TOLERANCE_ANGSTROM
        for clearance in clearances.values()
    ):
        raise ValueError("generated POSCAR does not provide the requested vacuum")

    original_to_poscar = mapping["original_to_poscar"]
    poscar_to_original = mapping["poscar_to_original"]
    expected_indices = set(range(len(plan.atoms)))
    if not isinstance(original_to_poscar, dict) or not isinstance(poscar_to_original, dict):
        raise ValueError("atom mapping is malformed")
    if set(original_to_poscar.values()) != expected_indices or set(
        poscar_to_original.values()
    ) != expected_indices:
        raise ValueError("atom mapping is not bijective")
    for original_index, poscar_index in original_to_poscar.items():
        if poscar_to_original.get(str(poscar_index)) != int(original_index):
            raise ValueError("atom mapping inverse is inconsistent")
    return written, clearances


def _write_poscar_atomically(path: Path, content: bytes) -> Path:
    """Write rendered POSCAR content to a same-directory temporary file."""

    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_bytes(content)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def render_xyz_to_poscar(
    input_xyz: str | Path,
    *,
    vacuum: float = DEFAULT_VACUUM_ANGSTROM,
    cell_mode: str = "auto",
    net_charge: int = 0,
    species_order: Iterable[str] | None = None,
    ordinary_xyz_only: bool = False,
) -> tuple[bytes, dict[str, object], dict[str, object]]:
    """Render and read back the existing embedding algorithm entirely in memory.

    Return exact POSCAR bytes, conversion metadata, and bidirectional atom mapping.
    The canonical entry point uses ``ordinary_xyz_only`` to reject periodic or
    extended input rather than dropping unsupported metadata. Legacy callers
    retain their existing reader behavior.
    """

    plan = _build_plan(
        input_xyz,
        vacuum=vacuum,
        cell_mode=cell_mode,
        net_charge=net_charge,
        species_order=species_order,
        ordinary_xyz_only=ordinary_xyz_only,
    )
    mapping = _mapping_record(plan)
    buffer = io.StringIO()
    ase_write(buffer, plan.atoms, format="vasp", direct=False, sort=False,
              vasp5=True, ignore_constraints=False)
    content = buffer.getvalue().encode("utf-8")
    written, clearances = _validate_written_poscar(io.StringIO(buffer.getvalue()), plan, mapping)

    cell_vectors = [
        [float(value) for value in row] for row in written.cell.array.tolist()
    ]
    metadata: dict[str, object] = {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "input_file": str(plan.input_path.resolve()),
        "input_sha256": plan.input_sha256,
        "input_size_bytes": plan.input_size_bytes,
        "output_sha256": hashlib.sha256(content).hexdigest(),
        "atom_count": len(plan.atoms),
        "composition": plan.composition,
        "species_order": list(plan.species_order),
        "atom_reordering_performed": plan.atom_reordering_performed,
        "net_charge": plan.net_charge,
        "requested_cell_mode": plan.requested_cell_mode,
        "resolved_cell_mode": plan.resolved_cell_mode,
        "cell_selection_reason": plan.cell_selection_reason,
        "warnings": list(plan.warnings),
        "vacuum_requested_angstrom": plan.vacuum,
        "actual_vacuum_clearances_angstrom": clearances,
        "cell_mode": plan.resolved_cell_mode,
        "cell_vectors_angstrom": cell_vectors,
        "molecular_bounding_box_angstrom": {
            "minimum": list(plan.bounding_minimum),
            "maximum": list(plan.bounding_maximum),
            "span": list(plan.molecular_spans),
        },
        "coordinate_mode": "Cartesian",
        "periodic_boundary_conditions": [True, True, True],
        "ase_version": ase.__version__,
        "validation": {
            "status": "PASSED",
            "tolerance_angstrom": VALIDATION_TOLERANCE_ANGSTROM,
            "checks": {
                "atom_count": True,
                "composition": True,
                "coordinate_order": True,
                "coordinates": True,
                "cell": True,
                "centered": True,
                "minimum_vacuum": True,
                "mapping_bijective": True,
            },
        },
    }
    return content, metadata, mapping


def xyz_to_poscar(
    input_xyz: str | Path,
    output_poscar: str | Path,
    *,
    vacuum: float = DEFAULT_VACUUM_ANGSTROM,
    cell_mode: str = "auto",
    net_charge: int = 0,
    species_order: Iterable[str] | None = None,
) -> dict[str, object]:
    """Legacy embedding with overwrite and adjacent mapping/metadata files.

    Vacuum is the minimum per-face clearance in ångström. The shared renderer
    centers the molecule, groups species stably, and selects a cubic cell for
    nonzero caller-supplied charge in auto mode. No charge is inferred.
    """

    content, metadata, mapping = render_xyz_to_poscar(
        input_xyz, vacuum=vacuum, cell_mode=cell_mode, net_charge=net_charge,
        species_order=species_order,
    )
    output_path = Path(output_poscar).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path = output_path.parent / MAPPING_FILENAME
    metadata_path = output_path.parent / METADATA_FILENAME
    if len({output_path.resolve(), mapping_path.resolve(), metadata_path.resolve()}) != 3:
        raise ValueError("POSCAR, mapping, and metadata output paths must be distinct")
    if output_path.resolve() == Path(input_xyz).expanduser().resolve():
        raise ValueError("XYZ input and POSCAR output paths must be distinct")
    temporary = _write_poscar_atomically(output_path, content)
    try:
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    metadata.update(output_file=str(output_path.resolve()), mapping_file=str(mapping_path.resolve()))
    atomic_write_json(mapping_path, mapping)
    atomic_write_json(metadata_path, metadata)
    return {**metadata, "conversion_metadata_file": str(metadata_path.resolve())}


__all__ = [
    "CELL_MODES",
    "DEFAULT_VACUUM_ANGSTROM",
    "inspect_xyz_to_poscar",
    "render_xyz_to_poscar",
    "xyz_to_poscar",
]
