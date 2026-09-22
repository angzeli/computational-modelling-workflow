"""Bounded, read-only checks for the four VASP preparation inputs.

Checks describe syntax and cross-file consistency, never scientific validity or
runtime acceptance. POSCAR block boundaries and source text are retained.
"""

from __future__ import annotations

import math
import json
from pathlib import Path
import re
from typing import Mapping

from cmw.structure.xyz import ELEMENTS
from cmw.core.preparation_publication import content_identity, inspect_publication
from cmw.periodic.vasp.potentials import inspect_potcar


INPUT_NAMES = ("INCAR", "KPOINTS", "POSCAR", "POTCAR")
BOOL_TAGS = frozenset("LASPH LCHARG LDIPOL LELF LHFCALC LNONCOLLINEAR LSORBIT LDAU LVHAR LWAVE ADDGRID LVTOT LUSE_VDW LAECHG LSCALAPACK".split())
INT_TAGS = frozenset("IBRION ICHARG IDIPOL ISIF ISMEAR ISPIN ISTART ISYM IVDW KPAR NCORE NPAR NELM NELMIN NELMDL NSW NBANDS LORBIT LMAXMIX LDAUTYPE LDAUPRINT IMIX INIMIX MAXMIX MIXPRE IALGO NKRED NKREDX NKREDY NKREDZ".split())
REAL_TAGS = frozenset("ENCUT EDIFF EDIFFG SIGMA POTIM AMIN AMIX BMIX AMIX_MAG BMIX_MAG WC NELECT NUPDOWN AEXX HFSCREEN TIME ENAUG SYMPREC".split())
STRING_TAGS = frozenset("SYSTEM ALGO PREC GGA METAGGA LREAL".split())
ARRAY_TAGS = frozenset("MAGMOM DIPOL LDAUL LDAUU LDAUJ".split())
KNOWN_TAGS = BOOL_TAGS | INT_TAGS | REAL_TAGS | STRING_TAGS | ARRAY_TAGS


def finding(code: str, severity: str, scope: str, message: str, **evidence: object) -> dict:
    return {"code": code, "severity": severity, "scope": scope,
            "message": message, "evidence": evidence}


def _finish(result: dict) -> dict:
    severities = {item["severity"] for item in result["findings"]}
    result["status"] = "invalid" if "error" in severities else "unsupported" if "unsupported" in severities else "valid"
    result["exit_code"] = {"valid": 0, "invalid": 1, "unsupported": 2}[result["status"]]
    return result


def _float(value: str) -> float:
    value = float(value.replace("D", "E").replace("d", "e"))
    if not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return value


def _determinant(cell: list[list[float]]) -> float:
    a, b, c = cell
    return (a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0]))


def parse_poscar(data: str | bytes) -> dict:
    """Inspect explicit-species POSCAR, preserving text, order and block counts.

    One positive scale, a negative target volume, and three positive Cartesian
    scale factors are supported. Coordinates are not wrapped or reordered.
    Velocity/predictor-corrector sections are reported as unsupported.
    """
    result = {"findings": []}
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        result["findings"].append(finding("poscar.encoding", "error", "POSCAR", "POSCAR must be UTF-8 text"))
        return _finish(result)
    result["raw_text"] = text
    lines = text.splitlines()
    try:
        if len(lines) < 8:
            raise ValueError("POSCAR header is incomplete")
        scales = [_float(value) for value in lines[1].split()]
        if len(scales) not in (1, 3):
            raise ValueError("Scale must contain one or three numbers")
        raw_cell = [[_float(value) for value in line.split()] for line in lines[2:5]]
        if any(len(row) != 3 for row in raw_cell):
            raise ValueError("Each cell vector must contain three numbers")
        determinant = _determinant(raw_cell)
        norms = math.prod(math.hypot(*row) for row in raw_cell)
        if not math.isfinite(determinant) or not math.isfinite(norms) or not norms or abs(determinant) <= 1e-12 * norms:
            raise ValueError("Cell is singular or numerically degenerate")
        if len(scales) == 1:
            if scales[0] == 0:
                raise ValueError("Scale cannot be zero")
            scale = scales[0] if scales[0] > 0 else (abs(scales[0]) / abs(determinant)) ** (1 / 3)
            factors = [scale] * 3
        else:
            if any(value <= 0 for value in scales):
                raise ValueError("Three scale factors must all be positive")
            factors = scales
        cell = [[value * factors[column] for column, value in enumerate(row)] for row in raw_cell]
        if any(not math.isfinite(value) for row in cell for value in row):
            raise ValueError("Scaled cell contains nonfinite values")
        if not math.isfinite(_determinant(cell)) or _determinant(cell) == 0:
            raise ValueError("Scaled cell is degenerate")
        species = lines[5].split()
        if species and all(value.isdecimal() for value in species):
            result["findings"].append(finding("poscar.implicit_species", "unsupported", "POSCAR", "VASP 4 implicit species are outside this checker"))
            return _finish(result)
        if not species or any(symbol not in ELEMENTS for symbol in species):
            raise ValueError("Explicit element symbols are required on the species line")
        tokens = lines[6].split()
        if len(tokens) != len(species) or any(not value.isdecimal() for value in tokens):
            raise ValueError("Species blocks and nonnegative integer counts must correspond")
        counts = [int(value) for value in tokens]
        atom_count = sum(counts)
        if atom_count <= 0:
            raise ValueError("At least one atom must be declared")
        index = 7
        selective = lines[index].strip().lower().startswith("s")
        if selective:
            index += 1
        if index >= len(lines):
            raise ValueError("Coordinate mode is missing")
        mode = lines[index].strip().lower()
        if mode.startswith("d"):
            coordinate_mode = "Direct"
        elif mode.startswith(("c", "k")):
            coordinate_mode = "Cartesian"
        else:
            raise ValueError("Coordinate mode must be Direct or Cartesian")
        index += 1
        if len(lines) - index < atom_count:
            raise ValueError("Fewer coordinate rows than declared atoms")
        coordinates, flags = [], []
        for offset in range(atom_count):
            tokens = lines[index + offset].split()
            required = 6 if selective else 3
            if len(tokens) < required:
                raise ValueError(f"Coordinate row {offset + 1} is incomplete")
            coordinates.append([_float(value) for value in tokens[:3]])
            if selective:
                if any(value.upper() not in ("T", "F") for value in tokens[3:6]):
                    raise ValueError(f"Coordinate row {offset + 1} has invalid selective-dynamics flags")
                flags.append([value.upper() == "T" for value in tokens[3:6]])
            if len(tokens) > required:
                result["findings"].append(finding("poscar.coordinate_suffix", "unsupported", "POSCAR", "Extra coordinate-row fields are not interpreted", line=index + offset + 1))
        remaining = [line for line in lines[index + atom_count:] if line.strip()]
        if remaining:
            result["findings"].append(finding("poscar.extra_sections", "unsupported", "POSCAR", "Additional coordinates, velocities or predictor-corrector sections are not interpreted", first_line=index + atom_count + 1))
        cartesian = [
            [sum(row[component] * cell[component][axis] for component in range(3))
             if coordinate_mode == "Direct" else row[axis] * factors[axis]
             for axis in range(3)]
            for row in coordinates
        ]
        if any(not math.isfinite(value) for row in cartesian for value in row):
            raise ValueError("Scaled Cartesian coordinates contain nonfinite values")
        result.update(species=species, counts=counts, atom_count=atom_count,
                      atom_species=[symbol for symbol, count in zip(species, counts) for _ in range(count)],
                      cell=cell, scale=scales, coordinate_mode=coordinate_mode,
                      coordinates=coordinates, cartesian_coordinates=cartesian,
                      selective_dynamics=flags if selective else None)
        result["findings"].append(finding("poscar.structure", "info", "POSCAR", "Supported cell, coordinates and ordered species blocks checked", atoms=atom_count, blocks=len(species)))
    except (ValueError, OverflowError) as error:
        result["findings"].append(finding("poscar.malformed", "error", "POSCAR", str(error)))
    return _finish(result)


def _segments(line: str) -> list[str]:
    """Split assignment separators, respecting quoted title text and comments."""
    parts, current, quote = [], [], None
    for char in line:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char in ("!", "#"):
            break
        elif char == ";":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote:
        raise ValueError("Unterminated quoted value")
    parts.append("".join(current))
    return parts


def _scalar(value: str) -> object:
    upper = value.upper()
    if upper in ("T", "TRUE", ".TRUE."):
        return True
    if upper in ("F", "FALSE", ".FALSE."):
        return False
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    try:
        return _float(value)
    except ValueError:
        if upper in ("NAN", "+NAN", "-NAN", "INF", "+INF", "-INF", "INFINITY", "+INFINITY", "-INFINITY"):
            raise ValueError("Nonfinite numeric value") from None
        return upper


def _value(key: str, text: str) -> object:
    if key == "SYSTEM":
        return text.strip().strip("\"'")
    if key in STRING_TAGS - {"LREAL"}:
        tokens = text.split()
        if len(tokens) != 1:
            raise ValueError("Tag requires one named setting")
        # Numeric-looking names such as GGA=91 are identifiers, not numbers.
        return tokens[0].upper()
    values = []
    for token in text.split():
        repeat = re.fullmatch(r"(\d+)\*(.+)", token)
        if repeat:
            count = int(repeat[1])
            if count > 100000 or count == 0:
                raise ValueError("Repeated-value count must be between 1 and 100000")
            values.extend([_scalar(repeat[2])] * count)
        else:
            values.append(_scalar(token))
    if not values:
        raise ValueError("Assignment value is empty")
    if key in ARRAY_TAGS:
        if any(type(value) not in (int, float) for value in values):
            raise ValueError("Array requires finite numeric values")
        if key == "LDAUL" and any(type(value) is not int for value in values):
            raise ValueError("LDAUL requires integer angular-momentum values")
        return values
    if key in KNOWN_TAGS and len(values) != 1:
        raise ValueError("Tag requires one value")
    value = values[0] if len(values) == 1 else values
    if key in BOOL_TAGS and type(value) is not bool:
        raise ValueError("Tag requires a boolean")
    if key in INT_TAGS and type(value) is not int:
        raise ValueError("Tag requires an integer")
    if key in REAL_TAGS and type(value) not in (int, float):
        raise ValueError("Tag requires a finite number")
    if key == "LREAL" and type(value) is not bool and value not in ("AUTO", "ON", "OFF"):
        raise ValueError("LREAL requires a boolean, Auto, On or Off")
    return value


def parse_incar(text: str) -> dict:
    """Parse bounded INCAR semantics; conflicting duplicates are never resolved."""
    result = {"findings": [], "settings": {}, "assignments": [], "unknown_tags": []}
    continuing = False
    for number, line in enumerate(text.splitlines(), 1):
        try:
            segments = _segments(line)
        except ValueError as error:
            result["findings"].append(finding("incar.syntax", "error", "INCAR", str(error), line=number))
            continue
        continuation = segments[-1].rstrip().endswith("\\")
        if continuation or continuing:
            if not continuing:
                result["findings"].append(finding("incar.continuation", "unsupported", "INCAR", "Backslash-continued assignments are not interpreted", line=number))
            if any(segment.strip() for segment in segments):
                continuing = continuation
            continue
        for segment in segments:
            if not segment.strip():
                continue
            match = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", segment)
            if not match:
                result["findings"].append(finding("incar.syntax", "error", "INCAR", "Expected KEY = VALUE assignment", line=number))
                continue
            key, raw = match[1].upper(), match[2]
            try:
                value = _value(key, raw)
            except ValueError as error:
                result["findings"].append(finding("incar.value", "error", "INCAR", str(error), tag=key, line=number))
                continue
            result["assignments"].append({"key": key, "value": value, "line": number, "raw_value": raw})
            if key in result["settings"]:
                same = result["settings"][key] == value
                result["findings"].append(finding("incar.duplicate_identical" if same else "incar.duplicate_conflict", "warning" if same else "error", "INCAR", "Duplicate assignment is semantically identical" if same else "Conflicting duplicate assignments are ambiguous", tag=key, line=number))
                continue
            result["settings"][key] = value
            if key not in KNOWN_TAGS:
                result["unknown_tags"].append(key)
                result["findings"].append(finding("incar.unknown_tag", "unassessed", "INCAR", "Tag retained but not validated by this bounded checker", tag=key))
    return _finish(result)


def parse_kpoints(text: str) -> dict:
    """Inspect regular automatic meshes; shifts are finite reciprocal offsets."""
    result = {"findings": []}
    lines = text.splitlines()
    try:
        if len(lines) < 3:
            raise ValueError("KPOINTS header is incomplete")
        count = int(lines[1].strip())
        scheme = lines[2].strip().lower()
        if count > 0 or not scheme.startswith(("g", "m")):
            result["findings"].append(finding("kpoints.mode", "unsupported", "KPOINTS", "Only automatic Gamma and Monkhorst-Pack regular meshes are assessed", declared_points=count, mode=lines[2].strip()))
            return _finish(result)
        if len(lines) < 4:
            raise ValueError("Regular mesh dimensions are missing")
        tokens = lines[3].split()
        if len(tokens) != 3 or any(not re.fullmatch(r"\+?\d+", value) or int(value) <= 0 for value in tokens):
            raise ValueError("Mesh dimensions must be three positive integers")
        shift = [_float(value) for value in lines[4].split()] if len(lines) >= 5 and lines[4].strip() else [0.0] * 3
        if len(shift) != 3:
            raise ValueError("Mesh shift must contain three finite numbers")
        if any(line.strip() for line in lines[5:]):
            result["findings"].append(finding("kpoints.extra_sections", "unsupported", "KPOINTS", "Additional KPOINTS content is not interpreted"))
        result.update(mode="Gamma" if scheme.startswith("g") else "Monkhorst-Pack", mesh=[int(value) for value in tokens], shift=shift)
        result["findings"].append(finding("kpoints.mesh", "info", "KPOINTS", "Regular mesh and shift checked"))
    except (ValueError, OverflowError) as error:
        result["findings"].append(finding("kpoints.malformed", "error", "KPOINTS", str(error)))
    return _finish(result)


def _cross_checks(inputs: dict, findings: list) -> None:
    settings = inputs.get("INCAR", {}).get("settings", {})
    structure = inputs.get("POSCAR", {})
    if settings.get("LNONCOLLINEAR") or settings.get("LSORBIT"):
        findings.append(finding("incar.noncollinear", "unsupported", "INCAR", "Noncollinear/SOC modes and their arrays are outside this checker"))
    elif "MAGMOM" in settings and "atom_count" in structure and len(settings["MAGMOM"]) != structure["atom_count"]:
        findings.append(finding("cross.magmom_length", "error", "INCAR/POSCAR", "Collinear MAGMOM must have one value per atom", expected=structure["atom_count"], actual=len(settings["MAGMOM"])))
    for key in ("LDAUL", "LDAUU", "LDAUJ"):
        if key in settings and "species" in structure and len(settings[key]) != len(structure["species"]):
            findings.append(finding("cross.species_array_length", "error", "INCAR/POSCAR", "Species-indexed array must follow every POSCAR species block, including repeats", tag=key, expected=len(structure["species"]), actual=len(settings[key])))
    if "DIPOL" in settings and len(settings["DIPOL"]) != 3:
        findings.append(finding("incar.dipol_length", "error", "INCAR", "DIPOL must have three components"))
    for key in ("ENCUT", "EDIFF", "SIGMA", "NELM", "NELMIN", "NCORE", "KPAR", "NBANDS"):
        if key in settings and settings[key] <= 0:
            findings.append(finding("incar.positive_value", "error", "INCAR", "Tag must be positive within the supported scope", tag=key, value=settings[key]))
    if "NSW" in settings and settings["NSW"] < 0:
        findings.append(finding("incar.ionic_steps", "error", "INCAR", "NSW must be nonnegative"))
    if settings.get("NSW", 0) > 0:
        if settings.get("IBRION") == -1:
            findings.append(finding("cross.ionic_mode", "error", "INCAR", "Positive ionic-step request conflicts with disabled ionic updates"))
        elif settings.get("IBRION") not in (1, 2, 3) or settings.get("ISIF") != 2:
            findings.append(finding("incar.calculation_mode", "unsupported", "INCAR", "Only explicit fixed-cell ionic relaxation is assessed"))
    elif "NSW" not in settings:
        findings.append(finding("incar.calculation_mode_unassessed", "unassessed", "INCAR", "Calculation mode is not explicitly established; defaults are not invented"))
    if settings.get("ISTART", 0) != 0 or settings.get("ICHARG", 2) != 2:
        findings.append(finding("incar.restart", "unsupported", "INCAR", "Electronic restart and frozen-density assumptions are outside this four-file checker", ISTART=settings.get("ISTART"), ICHARG=settings.get("ICHARG")))
    elif "ISTART" not in settings or "ICHARG" not in settings:
        findings.append(finding("incar.initialization_unassessed", "unassessed", "INCAR", "Fresh initialization is not fully explicit"))


def check_input_bytes(files: Mapping[str, bytes], expected_selection: dict | None = None) -> dict:
    """Check supplied immutable input bytes without reading or writing files."""
    result = {"schema_version": 1, "findings": [], "inputs": {}, "identities": {}}
    findings, inputs = result["findings"], result["inputs"]
    if expected_selection is not None and (
        not isinstance(expected_selection, dict)
        or ("datasets" in expected_selection and (
            not isinstance(expected_selection["datasets"], list)
            or not all(isinstance(item, dict) for item in expected_selection["datasets"])
            or not isinstance(expected_selection.get("library_release", {}), dict)
        ))
    ):
        findings.append(finding("selection.specification", "error", "POTCAR", "Expected selection must be a requirements object or structured ordered selection record"))
        expected_selection = None
    for name in INPUT_NAMES:
        if name not in files:
            findings.append(finding("bundle.missing_input", "error", name, "Required input is missing"))
            continue
        data = files[name]
        result["identities"][name] = content_identity(data)
        try:
            if name == "POTCAR":
                structure = inputs.get("POSCAR", {})
                is_record = expected_selection is not None and "datasets" in expected_selection
                requirements = expected_selection.get("requirements") if is_record else expected_selection
                release = expected_selection.get("library_release", {}).get("value") if is_record else None
                inputs[name] = inspect_potcar(data, species=structure.get("species"), counts=structure.get("counts"),
                                              requirements=requirements, library_release=release)
                if not inputs[name]["identity_valid"]:
                    findings.append(finding("selection.requirements", "error", "POTCAR", "Required potential identity or comparability is unestablished or differs", comparisons=inputs[name]["comparability"]))
            else:
                parser = {"INCAR": parse_incar, "POSCAR": parse_poscar, "KPOINTS": parse_kpoints}[name]
                inputs[name] = parser(data.decode("utf-8"))
                findings.extend(inputs[name]["findings"])
        except (ValueError, UnicodeDecodeError) as error:
            findings.append(finding("potcar.malformed" if name == "POTCAR" else "bundle.encoding", "error", name, str(error)))
    if "species" in inputs.get("POSCAR", {}) and "POTCAR" in inputs:
        findings.append(finding("cross.potential_sequence", "info", "POSCAR/POTCAR", "Complete ordered species-block and dataset sequences match"))
    if expected_selection is None:
        findings.append(finding("selection.unassessed", "unassessed", "POTCAR", "No expected selection or comparability requirements supplied; element matching is not exact selection verification"))
    elif "datasets" in expected_selection:
        _check_selection(expected_selection, inputs.get("POTCAR", {}), files.get("POTCAR"), findings)
    _cross_checks(inputs, findings)
    findings.append(finding("scientific_validity.unassessed", "unassessed", "bundle", "Physical model, convergence, runtime compatibility and scientific suitability are not assessed"))
    return _finish(result)


def _check_selection(expected: dict, potcar: dict, data: bytes | None, findings: list) -> None:
    # The public selection record is supplied by the existing POTCAR component.
    wanted = expected.get("datasets", [])
    actual = potcar.get("datasets", [])
    if not wanted:
        findings.append(finding("selection.requirements", "error", "POTCAR", "Expected selection must identify ordered datasets"))
        return
    if len(wanted) != len(actual):
        findings.append(finding("selection.count", "error", "POTCAR", "Expected dataset sequence has a different length", expected=len(wanted), actual=len(actual)))
        return
    for index, (requirement, observed) in enumerate(zip(wanted, actual)):
        for key in ("element", "label", "family", "dataset_date", "sha256"):
            if requirement.get(key) is not None and requirement[key] != observed.get(key):
                findings.append(finding("selection.identity", "error", "POTCAR", "Expected potential identity is absent or differs", block=index, field=key, expected=requirement[key], actual=observed.get(key)))
    if data is not None and expected.get("potcar_sha256") and expected["potcar_sha256"] != content_identity(data)["sha256"]:
        findings.append(finding("selection.content", "error", "POTCAR", "Assembled POTCAR content differs from expected selection"))


def check_inputs(directory: str | Path, record_path: str | Path | None = None,
                 expected_selection: dict | None = None) -> dict:
    """Read exactly the requested inputs; an external record is optional."""
    directory = Path(directory).expanduser().absolute()
    files, read_findings = {}, []
    try:
        for name in INPUT_NAMES:
            path = directory / name
            if path.is_symlink():
                read_findings.append(finding("bundle.symlink", "unsupported", name, "Input symlinks are not followed by this checker"))
            elif path.is_file():
                files[name] = path.read_bytes()
        extras = sorted(path.name for path in directory.iterdir() if path.name not in INPUT_NAMES)
        if extras:
            read_findings.append(finding("bundle.extra_files", "unsupported" if "KPOINTS_OPT" in extras else "warning", "bundle", "Directory contains entries outside the four-file preparation bundle", names=extras))
    except OSError as error:
        read_findings.append(finding("bundle.read", "error", "bundle", str(error)))
    record = None
    if record_path is not None:
        try:
            path = Path(record_path).expanduser().absolute()
            if path.resolve() != path or path.is_symlink():
                raise ValueError("External preparation-record symlinks are not followed")
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("record_kind") != "vasp-input-preparation":
                raise ValueError("Expected schema 1 vasp-input-preparation record")
            if expected_selection is None:
                expected_selection = record.get("selected_potentials")
        except (OSError, ValueError, RuntimeError) as error:
            read_findings.append(finding("record.schema", "error", "record", str(error)))
            record = None
    result = check_input_bytes(files, expected_selection)
    symlink_names = {item["scope"] for item in read_findings if item["code"] == "bundle.symlink"}
    result["findings"] = [item for item in result["findings"] if not (item["code"] == "bundle.missing_input" and item["scope"] in symlink_names)]
    result["directory"] = str(directory)
    result["findings"].extend(read_findings)
    if record_path is None:
        result["findings"].append(finding("record.unassessed", "unassessed", "record", "No external preparation record supplied; lineage and publication state are unassessed"))
    else:
        result["record"] = {"path": str(Path(record_path).expanduser().absolute())}
        result["findings"].extend(inspect_publication(record_path, input_path=directory)["findings"])
        if record is not None and record.get("prepared_inputs") != result["identities"]:
            result["findings"].append(finding("record.snapshot_mismatch", "error", "record", "The inspected four-file snapshot differs from recorded identities"))
    return _finish(result)
