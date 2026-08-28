"""Charge-aware ASE conversion of molecular XYZ files to VASP POSCAR."""

from __future__ import annotations

import math
import os
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

from .xyz import ELEMENTS


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


def _normalize_species_order(
    symbols: tuple[str, ...], requested: Iterable[str] | None
) -> tuple[str, ...]:
    """Validate an explicit order or retain unique elements by first occurrence."""

    present = tuple(dict.fromkeys(symbols))
    if requested is None:
        return present

    normalized: list[str] = []
    for raw_symbol in requested:
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            raise ValueError("species order entries must be non-empty element symbols")
        value = raw_symbol.strip()
        symbol = value[0].upper() + value[1:].lower()
        if symbol not in ELEMENTS:
            raise ValueError(f"invalid species-order element symbol: {raw_symbol!r}")
        normalized.append(symbol)

    duplicates = sorted(
        symbol for symbol, count in Counter(normalized).items() if count > 1
    )
    if duplicates:
        raise ValueError(
            "species order contains duplicate elements: " + ", ".join(duplicates)
        )

    absent = [symbol for symbol in normalized if symbol not in present]
    omitted = [symbol for symbol in present if symbol not in normalized]
    if absent:
        raise ValueError(
            "species order requests elements absent from the XYZ: " + ", ".join(absent)
        )
    if omitted:
        raise ValueError(
            "species order omits elements present in the XYZ: " + ", ".join(omitted)
        )
    return tuple(normalized)


def _read_single_xyz(path: Path) -> tuple[Atoms, str]:
    """Read exactly one molecular XYZ record with ASE and stable input evidence."""

    if not path.exists():
        raise FileNotFoundError(f"XYZ input does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"XYZ input is not a regular file: {path}")

    initial_sha256 = file_hash(path)
    try:
        records = ase_read(path, format="xyz", index=":")
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
    return atoms, initial_sha256


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
    source_atoms, input_sha256 = _read_single_xyz(input_path)
    original_symbols = tuple(source_atoms.get_chemical_symbols())
    selected_species = _normalize_species_order(original_symbols, species_order)
    poscar_to_original = tuple(
        index
        for element in selected_species
        for index, symbol in enumerate(original_symbols)
        if symbol == element
    )
    if sorted(poscar_to_original) != list(range(len(source_atoms))):
        raise ValueError("species grouping did not produce a bijective atom mapping")

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
    path: Path, plan: _ConversionPlan, mapping: dict[str, object]
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


def _write_poscar_atomically(path: Path, atoms: Atoms) -> Path:
    """Write Cartesian VASP5 POSCAR content to a same-directory temporary file."""

    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        ase_write(
            temporary,
            atoms,
            format="vasp",
            direct=False,
            sort=False,
            vasp5=True,
            ignore_constraints=False,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def xyz_to_poscar(
    input_xyz: str | Path,
    output_poscar: str | Path,
    *,
    vacuum: float = DEFAULT_VACUUM_ANGSTROM,
    cell_mode: str = "auto",
    net_charge: int = 0,
    species_order: Iterable[str] | None = None,
) -> dict[str, object]:
    """Convert one molecular XYZ to a centered periodic POSCAR.

    ``vacuum`` is the minimum clearance in ångström from the outermost atomic
    coordinate to each cell face. Atoms are grouped by element for VASP while
    retaining their relative order within each group. The returned record is
    also written as ``conversion_metadata.json`` beside the POSCAR.

    ``net_charge`` is the caller-supplied formal molecular charge; it is never
    inferred from composition. In ``auto`` mode, neutral molecules use an
    orthorhombic cell and charged molecules use a cubic cell.

    Raises:
        OSError: If an input or output file cannot be accessed.
        ValueError: If validation, species grouping, or POSCAR read-back fails.
    """

    plan = _build_plan(
        input_xyz,
        vacuum=vacuum,
        cell_mode=cell_mode,
        net_charge=net_charge,
        species_order=species_order,
    )
    output_path = Path(output_poscar).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path = output_path.parent / MAPPING_FILENAME
    metadata_path = output_path.parent / METADATA_FILENAME
    resolved_destinations = {
        output_path.resolve(),
        mapping_path.resolve(),
        metadata_path.resolve(),
    }
    if len(resolved_destinations) != 3:
        raise ValueError("POSCAR, mapping, and metadata output paths must be distinct")
    if output_path.resolve() == plan.input_path.resolve():
        raise ValueError("XYZ input and POSCAR output paths must be distinct")

    mapping = _mapping_record(plan)
    temporary = _write_poscar_atomically(output_path, plan.atoms)
    try:
        written, clearances = _validate_written_poscar(temporary, plan, mapping)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    cell_vectors = [
        [float(value) for value in row] for row in written.cell.array.tolist()
    ]
    metadata: dict[str, object] = {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "input_file": str(plan.input_path.resolve()),
        "output_file": str(output_path.resolve()),
        "input_sha256": plan.input_sha256,
        "output_sha256": file_hash(output_path),
        "atom_count": len(plan.atoms),
        "composition": plan.composition,
        "species_order": list(plan.species_order),
        "atom_reordering_performed": plan.atom_reordering_performed,
        "mapping_file": str(mapping_path.resolve()),
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
    atomic_write_json(mapping_path, mapping)
    atomic_write_json(metadata_path, metadata)
    return {**metadata, "conversion_metadata_file": str(metadata_path.resolve())}


__all__ = [
    "CELL_MODES",
    "DEFAULT_VACUUM_ANGSTROM",
    "inspect_xyz_to_poscar",
    "xyz_to_poscar",
]
