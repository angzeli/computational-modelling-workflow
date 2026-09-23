"""Bounded ordered core-CIF import, with explicit crystallographic orbit evidence."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
import math
import re

import ase
from ase import Atoms
from ase.io import read, write
import gemmi
import numpy as np

from .ordering import group_species
from .periodic import PeriodicAtom, PeriodicStructure
from .xyz import ELEMENTS

DUPLICATE_TOLERANCE = 1e-8  # Maximum component of a periodic fractional difference.
EXACT_IMAGE_TOLERANCE = 1e-12


class CifImportError(ValueError):
    def __init__(self, message: str, *, code: str = "unsupported_cif", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class CifImportResult:
    structure: PeriodicStructure
    metadata: dict
    poscar: bytes


def _fail(message, code="unsupported_cif", **details):
    raise CifImportError(message, code=code, details=details)


def _text(token):
    return gemmi.cif.as_string(token)


def _number(token, name):
    value = gemmi.cif.as_number(token)
    if not math.isfinite(value):
        _fail(f"{name} must be a finite CIF number")
    return value


def _tags(block):
    tags = []
    for item in block:
        if item.frame is not None:
            _fail("Save frames are not supported in structural CIF blocks")
        if item.pair is not None:
            tags.append(item.pair[0].lower())
        elif item.loop is not None:
            tags.extend(tag.lower() for tag in item.loop.tags)
    return tags


def _values(block, tag):
    return list(block.find_values(tag))


def _scalar(block, aliases, required=False):
    found = [(tag, _values(block, tag)) for tag in aliases if _values(block, tag)]
    if len(found) > 1:
        _fail(f"Multiple declarations of {aliases[0]} are unsupported")
    if not found:
        if required:
            _fail(f"Missing required {aliases[0]}")
        return None
    tag, values = found[0]
    if len(values) != 1:
        _fail(f"{tag} must contain exactly one value")
    return values[0]


def _cell(block):
    names = [f"_cell_length_{x}" for x in "abc"] + [f"_cell_angle_{x}" for x in ("alpha", "beta", "gamma")]
    raw = {tag: _scalar(block, [tag], True) for tag in names}
    values = [_number(raw[tag], tag) for tag in names]
    a, b, c, alpha, beta, gamma = values
    if min(a, b, c) <= 0 or not all(0 < angle < 180 for angle in values[3:]):
        _fail("Cell requires positive lengths and angles strictly between 0 and 180 degrees")
    ca, cb, cg = (math.cos(math.radians(x)) for x in values[3:])
    sg = math.sin(math.radians(gamma))
    cy = (ca - cb * cg) / sg
    cz2 = 1 - cb * cb - cy * cy
    if cz2 <= 0:
        _fail("Cell is degenerate or has an invalid metric")
    cell = np.array([[a, 0, 0], [b * cg, b * sg, 0], [c * cb, c * cy, c * math.sqrt(cz2)]])
    volume = float(np.linalg.det(cell))
    if not np.all(np.isfinite(cell)) or not math.isfinite(volume) or volume <= 0:
        _fail("Cell must have finite positive volume")
    return cell, dict(zip(("a", "b", "c", "alpha", "beta", "gamma"), values)), raw


def _op_key(op):
    return op.wrap().triplet()


def _op_set(ops):
    return frozenset(_op_key(op) for op in ops)


def _symmetry(block, cell):
    explicit = [(tag, _values(block, tag)) for tag in ("_space_group_symop_operation_xyz", "_symmetry_equiv_pos_as_xyz") if _values(block, tag)]
    if len(explicit) > 1:
        _fail("Multiple symmetry-operation tables are unsupported")
    hall = _scalar(block, ["_space_group_name_hall", "_symmetry_space_group_name_hall"])
    hm = _scalar(block, ["_space_group_name_h-m_alt", "_symmetry_space_group_name_h-m"])
    number = _scalar(block, ["_space_group_it_number", "_symmetry_int_tables_number"])
    declarations = {"hall": _text(hall) if hall else None, "hermann_mauguin": _text(hm) if hm else None, "it_number": None}
    candidates = list(gemmi.spacegroup_table())
    if number is not None:
        n = _number(number, "Space-group number")
        if n != int(n) or not 1 <= n <= 230:
            _fail("Space-group number must be an integer from 1 to 230")
        declarations["it_number"] = int(n)
        candidates = [sg for sg in candidates if sg.number == n]
    if hm is not None:
        norm = lambda name: re.sub(r"\s+", "", name).lower()
        candidates = [sg for sg in candidates if norm(_text(hm)) in {norm(sg.hm), norm(sg.xhm()), norm(sg.short_name())}]
        if not candidates:
            _fail("Unrecognized or inconsistent Hermann-Mauguin declaration")
    hall_ops = None
    if hall is not None:
        try:
            hall_ops = list(gemmi.symops_from_hall(_text(hall)))
        except (RuntimeError, ValueError) as exc:
            _fail(f"Invalid Hall declaration: {exc}")
        candidates = [sg for sg in candidates if _op_set(sg.operations()) == _op_set(hall_ops)]
        if (hm is not None or number is not None) and not candidates:
            _fail("Hall and other space-group declarations conflict")
    raw_ops = []
    mode = "explicit_operations"
    if explicit:
        tag, tokens = explicit[0]
        column = block.find_values(tag)
        loop = column.get_loop()
        if loop is None:
            _fail("Symmetry operations must be supplied in a loop")
        id_tag = "_space_group_symop_id" if tag.startswith("_space_group") else "_symmetry_equiv_pos_site_id"
        ids = _values(block, id_tag)
        if ids and (len(ids) != len(tokens) or id_tag not in [t.lower() for t in loop.tags]):
            _fail("Symmetry IDs must belong to the operation loop")
        for index, token in enumerate(tokens):
            try:
                op = gemmi.Op(_text(token)).wrap()
            except (RuntimeError, ValueError) as exc:
                _fail(f"Malformed or unsupported symmetry operation {index}: {exc}")
            raw_ops.append((op, {"row_index": index, "source_id": _text(ids[index]) if ids else None, "token": token}))
    else:
        mode = "declared_space_group"
        if hall_ops is not None:
            ops = hall_ops
        else:
            if hm is None and number is None:
                _fail("Symmetry information is required; missing symmetry is not assumed to be P1")
            unique = {_op_set(sg.operations()): sg for sg in candidates}
            if len(unique) != 1:
                _fail("Space-group setting is ambiguous; provide a setting-qualified name, Hall symbol or complete explicit operations")
            ops = list(next(iter(unique.values())).operations())
        raw_ops = [(op, {"derived_from": declarations}) for op in ops]
    grouped = {}
    metric = cell @ cell.T
    metric /= np.max(np.abs(metric))
    for op, evidence in raw_ops:
        rotation = np.array(op.rot, dtype=float) / op.DEN
        if not np.array_equal(rotation, np.rint(rotation)) or not np.isclose(abs(np.linalg.det(rotation)), 1, atol=1e-12, rtol=0):
            _fail("Symmetry operation must have an integral unimodular rotation")
        if not np.allclose(rotation.T @ metric @ rotation, metric, atol=1e-7, rtol=1e-7):
            _fail("Symmetry operations do not preserve the source cell metric")
        key = _op_key(op)
        if key not in grouped:
            grouped[key] = (op, [])
        grouped[key][1].append(evidence)
    keys = set(grouped)
    if "x,y,z" not in keys:
        _fail("The complete operation list must contain identity")
    # Never interpret a partial list as generators or silently complete a group.
    for left, _ in grouped.values():
        for right, _ in grouped.values():
            if _op_key(left.combine(right)) not in keys:
                _fail("Symmetry operation list is incomplete (not closed under composition)")
    if hall_ops is not None and keys != _op_set(hall_ops):
        _fail("Explicit operations conflict with the Hall declaration")
    if (hm is not None or number is not None) and not any(keys == _op_set(sg.operations()) for sg in candidates):
        _fail("Explicit operations conflict with the declared space-group setting")
    ordered = sorted(keys, key=lambda value: (value != "x,y,z", value))
    operations = []
    for index, key in enumerate(ordered):
        op, evidence = grouped[key]
        operations.append((op, {"id": f"op{index}", "xyz": key, "declarations": evidence}))
    return operations, {"mode": mode, "declarations": declarations}


def _species(type_token, label):
    if type_token is not None:
        symbol = _text(type_token)
        match = re.fullmatch(r"([A-Z][a-z]?)(?:[1-9][0-9]*[+-])?", symbol)
        element = match.group(1) if match else None
    else:
        match = re.fullmatch(r"([A-Z][a-z]?)[0-9]*", label or "")
        element = match.group(1) if match else None
    if element not in ELEMENTS:
        _fail(f"Ambiguous or unsupported elemental identity: type={type_token!r}, label={label!r}")
    return element


def _canonical(values):
    return np.array([round(float(value) % 1.0, 12) % 1.0 for value in values])


def _distance(left, right):
    delta = np.asarray(left) - np.asarray(right)
    return float(np.max(np.abs(delta - np.rint(delta))))


def _check_periodic_equivalence(source, canonical, cell):
    delta = np.asarray(source) - np.asarray(canonical)
    residual = delta - np.rint(delta)
    if np.max(np.abs(residual)) > 1e-10 or np.linalg.norm(residual @ cell) > 1e-8:
        _fail("Coordinate canonicalization exceeds periodic Cartesian equivalence tolerance")


def _sites(block, tags):
    required = [f"_atom_site_fract_{axis}" for axis in "xyz"]
    columns = {tag: block.find_values(tag) for tag in required}
    if any(not len(column) for column in columns.values()):
        _fail("A structural block requires fractional x, y, z coordinates")
    loop = columns[required[0]].get_loop()
    if loop is None or not all(tag in [t.lower() for t in loop.tags] for tag in required):
        _fail("Fractional coordinates must share one atom-site loop")
    loop_tags = [tag.lower() for tag in loop.tags]
    site_tags = [tag for tag in tags if tag.startswith("_atom_site_") and not tag.startswith("_atom_site_aniso_")]
    if any(tag not in loop_tags for tag in site_tags):
        _fail("Alternate or separate atom-site tables are unsupported")
    count = len(columns[required[0]])
    optional = ["label", "type_symbol", "occupancy", "disorder_assembly", "disorder_group", "symmetry_multiplicity", "calc_flag", "refinement_flags", "attached_hydrogens"]
    fields = {name: _values(block, f"_atom_site_{name}") for name in optional}
    if any(values and len(values) != count for values in fields.values()):
        _fail("Atom-site columns have inconsistent row counts")
    seen_labels = set()
    sites = []
    for index in range(count):
        label = _text(fields["label"][index]) if fields["label"] else None
        if label in ("", "?", "."):
            label = None
        if label is not None and label in seen_labels:
            _fail("Duplicate atom-site labels are ambiguous")
        if label is not None:
            seen_labels.add(label)
        type_token = fields["type_symbol"][index] if fields["type_symbol"] else None
        element = _species(type_token, label)
        for name in ("disorder_assembly", "disorder_group"):
            if fields[name] and fields[name][index] != ".":
                _fail(f"Unresolved disorder at source site {index}: {name}")
        if fields["calc_flag"] and fields["calc_flag"][index] != "." and _text(fields["calc_flag"][index]) not in ("d", "c", "calc"):
            _fail("Unsupported dummy or unresolved calculated-site flag")
        if fields["refinement_flags"] and any(flag in _text(fields["refinement_flags"][index]).upper() for flag in ("D", "P")):
            _fail("Disorder or partial-occupancy refinement flags require an explicit model")
        if fields["attached_hydrogens"] and _number(fields["attached_hydrogens"][index], "Attached hydrogens") != 0:
            _fail("Implicit attached hydrogens lack coordinates and cannot be imported as a complete ordered structure")
        occupancy_token = fields["occupancy"][index] if fields["occupancy"] else None
        occupancy = _number(occupancy_token, "Occupancy") if occupancy_token is not None else 1.0
        if occupancy != 1.0:
            _fail(f"Only fully occupied ordered sites are supported (site {index}, occupancy {occupancy})")
        raw_fractional = [columns[tag][index] for tag in required]
        fractional = [_number(token, f"Fractional coordinate of site {index}") for token in raw_fractional]
        multiplicity = None
        if fields["symmetry_multiplicity"]:
            multiplicity = _number(fields["symmetry_multiplicity"][index], "Site multiplicity")
            if multiplicity <= 0 or multiplicity != int(multiplicity):
                _fail("Declared site multiplicity must be a positive integer")
            multiplicity = int(multiplicity)
        sites.append({"index": index, "label": label, "type_symbol": _text(type_token) if type_token else None,
                      "element": element, "fractional": fractional, "fractional_tokens": raw_fractional,
                      "canonical_fractional": _canonical(fractional).tolist(), "occupancy": occupancy,
                      "occupancy_evidence": {"token": occupancy_token, "basis": "explicit" if occupancy_token is not None else "coreCIF_dictionary_default_1"},
                      "declared_multiplicity": multiplicity})
    return sites


def read_ordered_cif(data: bytes, *, block: str | None = None) -> CifImportResult:
    """Parse a source byte snapshot without file writes or scientific model choices."""
    try:
        text = data.decode("utf-8")
        if text.lstrip().startswith("#\\#CIF_2.0"):
            _fail("CIF 2.0 is unsupported; this importer accepts ordinary core CIF 1.1")
        document = gemmi.cif.read_string(text, check_level=2)
    except (UnicodeError, RuntimeError, ValueError) as exc:
        if isinstance(exc, CifImportError):
            raise
        _fail(f"Invalid CIF syntax: {exc}", "invalid_cif")
    available = []
    for candidate in document:
        tags = _tags(candidate)
        structural = any(tag.startswith(("_atom_site_", "_atom_site.", "_cell_", "_cell.")) for tag in tags)
        available.append({"name": candidate.name, "structural_candidate": structural, "evidence": "atom-site or cell tags" if structural else "no structural tags"})
    names = [item["name"] for item in available if item["structural_candidate"]]
    if block is None and len(names) != 1:
        _fail("Select one structural CIF block with --block" if names else "No structural CIF block found", "block_selection_required", available_blocks=available)
    selected = block if block is not None else names[0]
    if selected not in names:
        _fail(f"Selected structural CIF block does not exist: {selected}", "block_not_found", available_blocks=available)
    source = next(item for item in document if item.name == selected)
    tags = _tags(source)
    unsupported = [tag for tag in tags if any(part in tag for part in ("magn", "_moment", "fourier", "modulation", "subsystem", "_ssg", "_superspace", "_alt_id", "_model", "cartn", "_space_group_it_coordinate_system_code")) or tag.startswith(("_atom_site.", "_cell."))]
    if unsupported:
        _fail("Unsupported magnetic, alternate, transformed or non-core structural model", tags=unsupported)
    cell, parameters, cell_tokens = _cell(source)
    operations, symmetry_evidence = _symmetry(source, cell)
    sites = _sites(source, tags)
    expanded = []
    for site in sites:
        _check_periodic_equivalence(site["fractional"], site["canonical_fractional"], cell)
        orbit = []
        for operation, evidence in operations:
            image = operation.apply_to_xyz(site["canonical_fractional"])
            position = _canonical(image)
            _check_periodic_equivalence(image, position, cell)
            matches = [atom for atom in orbit if _distance(atom["fractional"], position) <= DUPLICATE_TOLERANCE]
            if matches:
                if _distance(matches[0]["fractional"], position) > EXACT_IMAGE_TOLERANCE:
                    _fail("Near-special-position images are ambiguous within the fractional tolerance; explicit model resolution is required")
                matches[0]["symmetry_operation_ids"].append(evidence["id"])
            else:
                orbit.append({"element": site["element"], "fractional": position, "source_site_index": site["index"], "symmetry_operation_ids": [evidence["id"]]})
        if site["declared_multiplicity"] is not None and len(orbit) != site["declared_multiplicity"]:
            _fail(f"Declared multiplicity does not match expanded orbit of site {site['index']}")
        if any(len(atom["symmetry_operation_ids"]) * len(orbit) != len(operations) for atom in orbit):
            _fail("Orbit and stabilizer multiplicities are inconsistent")
        for atom in orbit:
            if any(_distance(atom["fractional"], other["fractional"]) <= DUPLICATE_TOLERANCE for other in expanded):
                _fail("Distinct source-site orbits overlap; mixed or duplicate site models are unsupported")
            atom["expanded_index"] = len(expanded)
            expanded.append(atom)
        site["expanded_indices"] = [atom["expanded_index"] for atom in orbit]
        site["multiplicity"] = len(orbit)
    species_order, output_to_expanded = group_species(tuple(atom["element"] for atom in expanded))
    atoms = tuple(PeriodicAtom(element=expanded[i]["element"], fractional=tuple(float(v) for v in expanded[i]["fractional"]),
                              cartesian=tuple(float(v) for v in expanded[i]["fractional"] @ cell),
                              source_site_index=expanded[i]["source_site_index"], expanded_index=i,
                              symmetry_operation_ids=tuple(expanded[i]["symmetry_operation_ids"])) for i in output_to_expanded)
    structure = PeriodicStructure(tuple(tuple(float(v) for v in row) for row in cell), atoms, sha256(data).hexdigest(), selected)
    for site in sites:
        site["output_indices"] = [i for i, atom in enumerate(atoms) if atom.source_site_index == site["index"]]
    ase_atoms = Atoms(symbols=[atom.element for atom in atoms], cell=cell, scaled_positions=[atom.fractional for atom in atoms], pbc=True)
    buffer = StringIO()
    write(buffer, ase_atoms, format="vasp", direct=True, sort=False, vasp5=True)
    poscar = buffer.getvalue().encode("utf-8")
    reread = read(StringIO(poscar.decode()), format="vasp")
    if not np.allclose(reread.cell.array, cell, rtol=0, atol=1e-10) or reread.get_volume() <= 0:
        _fail("POSCAR readback changed the represented source cell", "validation_failed")
    if reread.get_chemical_symbols() != [atom.element for atom in atoms]:
        _fail("POSCAR readback changed atom count, species or output ordering", "validation_failed")
    if any(_distance(a, b.fractional) > 1e-10 for a, b in zip(reread.get_scaled_positions(wrap=False), atoms)):
        _fail("POSCAR readback changed periodic coordinates", "validation_failed")
    for site in sites:
        if not site["output_indices"] or len(site["output_indices"]) != site["multiplicity"]:
            _fail("Incomplete source-site coverage", "validation_failed")
    known = {"_atom_site_label", "_atom_site_type_symbol", "_atom_site_fract_x", "_atom_site_fract_y", "_atom_site_fract_z", "_atom_site_occupancy", "_atom_site_disorder_assembly", "_atom_site_disorder_group", "_atom_site_symmetry_multiplicity"}
    assessed = known | {f"_cell_length_{axis}" for axis in "abc"} | {f"_cell_angle_{angle}" for angle in ("alpha", "beta", "gamma")}
    assessed |= {"_space_group_name_hall", "_symmetry_space_group_name_hall", "_space_group_name_h-m_alt", "_symmetry_space_group_name_h-m", "_space_group_it_number", "_symmetry_int_tables_number", "_space_group_symop_operation_xyz", "_symmetry_equiv_pos_as_xyz", "_space_group_symop_id", "_symmetry_equiv_pos_site_id"}
    metadata = {"importer": {"name": "cmw.structure.cif", "contract_version": 1},
                "parser": {"name": "gemmi", "version": gemmi.__version__}, "poscar_writer": {"name": "ase", "version": ase.__version__},
                "selected_block": selected, "available_blocks": available,
                "source_cell": {"parameters": parameters, "tokens": cell_tokens, "matrix": cell.tolist()}, "output_cell": cell.tolist(),
                "cell_policy": "Preserve represented a,b,c basis and metric; a along +x, b in xy with +y, c with +z. CIF metrics alone do not specify absolute Cartesian orientation.",
                "source_sites": sites, "symmetry_operations": [evidence for _, evidence in operations], "symmetry_evidence": symmetry_evidence,
                "expansion_policy": {"operation_order": "identity first, then canonical triplet lexical order", "duplicate_tolerance_fractional": DUPLICATE_TOLERANCE,
                                     "exact_image_tolerance_fractional": EXACT_IMAGE_TOLERANCE, "near_special_images": "reject ambiguous near-duplicates", "distinct_source_orbits": "never merge; reject overlaps"},
                "coordinate_policy": {"interval": "[0,1)", "decimal_places": 12, "source_coordinates_retained": True},
                "ordering": {"species": list(species_order), "within_species": "stable source row then canonical operation", "output_to_expanded": list(output_to_expanded), "index_base": 0},
                "representation_transformations": ["periodic fractional wrapping into [0,1), quantized to 12 decimals", "stable species grouping for POSCAR"],
                "validation": {"status": "passed", "scope": "ordered periodic import integrity", "cell_preserved": True, "volume_angstrom3": float(np.linalg.det(cell)),
                               "atom_count": len(atoms), "composition": dict(Counter(atom.element for atom in atoms)), "orbit_multiplicities": [site["multiplicity"] for site in sites],
                               "poscar_readback": "ASE VASP reader", "periodic_cartesian_tolerance_angstrom": 1e-8, "expanded_output_bijection": True, "source_site_coverage": True, "duplicate_expanded_atoms": False},
                "unassessed_source_properties": [tag for tag in tags if tag not in assessed],
                "scientifically_unassessed": ["DFT suitability", "phase stability", "magnetic order", "oxidation state", "supercell, slab or interface suitability"]}
    return CifImportResult(structure, metadata, poscar)
