"""Selective, read-only spectra and sampled occupation-aware band extrema.

These readers retain printed numbers and source snapshots. They do not establish
that a separate spectrum belongs to an OUTCAR evaluation; the analysis context
must prove that correspondence before promoting any result to final evidence.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
import xml.etree.ElementTree as ET

from cmw.analysis.electronic import finite_array
from .result_sources import SnapshotReader
from .results import number


def _value(text: str, kind: str | None = None):
    token = text.strip()
    if kind == "logical":
        return {"T": True, "F": False, ".TRUE.": True, ".FALSE.": False}.get(token.upper(), token)
    if kind == "string":
        return token
    parsed = number(token)
    if parsed["value"] is None:
        return token
    if kind == "int":
        if int(parsed["value"]) != parsed["value"]:
            raise ValueError("noninteger XML integer setting")
        return int(parsed["value"])
    return parsed["value"]


def _numbers(text: str, count: int) -> list[float]:
    tokens = text.split()
    if len(tokens) != count:
        raise ValueError(f"expected {count} numeric columns")
    values = [number(token)["value"] for token in tokens]
    return finite_array(values, ndim=1, name="native numeric row").tolist()


def read_xml(path: str | Path, evaluation_index: int | None = None, *,
             include_spectrum: bool = True) -> dict:
    """Stream selected XML evidence, discarding DOS/projector/grid payloads.

    Only the selected calculation's eigenvalues survive; when no selection is
    supplied the latest chronological calculation is selected, even if partial.
    Earlier calculation identities and energies remain available for matching.
    No ElementTree subtree or complete source text is retained.
    """
    if evaluation_index is not None and (isinstance(evaluation_index, bool)
                                        or not isinstance(evaluation_index, int) or evaluation_index < 0):
        raise ValueError("evaluation_index must be a nonnegative integer")
    reader = SnapshotReader(path, "vasprun.xml")
    parser = ET.XMLPullParser(events=("start", "end"))
    stack: list[tuple[str, dict]] = []
    elements: list[ET.Element] = []
    result = {"format": "xml", "settings": {}, "version": None, "kpoints": [],
              "calculations": [], "complete": False, "findings": [],
              "selected_calculation": None, "spectrum": None, "elements": []}
    coordinates, coordinate_tokens, weights = [], [], []
    calculation = None
    structure = None
    spin_index = kpoint_index = None
    current_scstep = None
    effective_settings = set()
    closed_root = False
    atom_row = None

    def consume_events():
        nonlocal calculation, structure, spin_index, kpoint_index, current_scstep, closed_root, atom_row
        for event, element in parser.read_events():
            tag = element.tag
            if event == "start":
                stack.append((tag, dict(element.attrib)))
                elements.append(element)
                if len(stack) > 64:
                    raise ValueError("XML nesting exceeds the supported depth")
                if tag == "calculation":
                    if calculation is not None:
                        raise ValueError("nested XML calculations")
                    index = len(result["calculations"])
                    if evaluation_index is None:
                        for previous in result["calculations"]:
                            previous["spectrum"] = None
                    calculation = {"index": index, "scstep_count": 0, "scsteps": [],
                                   "energies": {}, "geometry": None, "fermi": None,
                                   "complete": False, "spectrum": None}
                    if include_spectrum and (evaluation_index is None or evaluation_index == index):
                        calculation["spectrum"] = {"format": "xml", "eigenvalues": [], "findings": []}
                    result["calculations"].append(calculation)
                elif tag == "structure":
                    structure = {"cell": [], "cell_tokens": [], "fractional": [],
                                 "fractional_tokens": [], "name": element.get("name"),
                                 "elements": list(result["elements"])}
                elif tag == "rc" and any(t == "array" and a.get("name") == "atoms" for t, a in stack):
                    atom_row = []
                elif tag == "scstep" and calculation is not None:
                    current_scstep = {"energies": {}}
                elif (tag == "set" and any(t == "eigenvalues" for t, _ in stack)
                      and not any(t == "projected" for t, _ in stack)):
                    comment = element.get("comment", "")
                    if match := re.fullmatch(r"spin\s+(\d+)", comment):
                        spin_index = int(match[1]) - 1
                        kpoint_index = None
                    elif match := re.fullmatch(r"kpoint\s+(\d+)", comment):
                        kpoint_index = int(match[1]) - 1
                        if calculation is not None and calculation["spectrum"] is not None:
                            eigenvalues = calculation["spectrum"]["eigenvalues"]
                            if spin_index is None or spin_index < 0 or kpoint_index < 0:
                                raise ValueError("invalid XML spin/kpoint labels")
                            if spin_index > len(eigenvalues):
                                raise ValueError("XML spin labels are not sequential")
                            if spin_index == len(eigenvalues):
                                eigenvalues.append([])
                            if kpoint_index != len(eigenvalues[spin_index]):
                                raise ValueError("XML kpoint labels are not sequential")
                            eigenvalues[spin_index].append([])
                continue
            tags = [t for t, _ in stack]
            text = element.text or ""
            name = element.get("name")
            if tag == "i" and name:
                if "generator" in tags and name == "version":
                    result["version"] = text.strip()
                elif "parameters" in tags or "incar" in tags:
                    if "parameters" in tags or name not in effective_settings:
                        result["settings"][name.upper()] = _value(text, element.get("type"))
                    if "parameters" in tags:
                        effective_settings.add(name)
                elif calculation is not None and name == "efermi" and "dos" in tags:
                    calculation["fermi"] = number(text.strip())
                elif calculation is not None and len(tags) >= 3 and tags[-2] == "energy":
                    if current_scstep is not None:
                        current_scstep["energies"][name] = number(text.strip())
                    elif tags[-3] == "calculation":
                        calculation["energies"][name] = number(text.strip())
            elif tag == "v" and len(stack) >= 2 and stack[-2][0] == "varray":
                array_name = stack[-2][1].get("name")
                if "kpoints" in tags and array_name == "kpointlist":
                    coordinates.append(_numbers(text, 3))
                    coordinate_tokens.append([number(token) for token in text.split()])
                elif "kpoints" in tags and array_name == "weights":
                    weights.append(_numbers(text, 1)[0])
                elif structure is not None and array_name == "basis":
                    structure["cell"].append(_numbers(text, 3))
                    structure["cell_tokens"].append([number(token) for token in text.split()])
                elif structure is not None and array_name == "positions":
                    structure["fractional"].append(_numbers(text, 3))
                    structure["fractional_tokens"].append([number(token) for token in text.split()])
            elif tag == "c" and atom_row is not None:
                atom_row.append(text.strip())
            elif tag == "rc" and atom_row is not None:
                if not atom_row or not re.fullmatch(r"[A-Z][a-z]?", atom_row[0]):
                    raise ValueError("invalid XML atom identity")
                result["elements"].append(atom_row[0])
                atom_row = None
            elif tag == "r" and "eigenvalues" in tags and "projected" not in tags and calculation is not None:
                spectrum = calculation["spectrum"]
                if spectrum is not None:
                    tokens = text.split()
                    if len(tokens) != 2 or spin_index is None or kpoint_index is None:
                        raise ValueError("unsupported XML eigenvalue row")
                    band = spectrum["eigenvalues"][spin_index][kpoint_index]
                    band.append({"energy": number(tokens[0]), "occupation": number(tokens[1]),
                                 "native_band_label": len(band) + 1})
            elif tag == "structure":
                if calculation is not None:
                    calculation["geometry"] = structure
                elif structure and structure["name"] in {"initialpos", "finalpos"}:
                    result[structure["name"]] = structure
                structure = None
            elif tag == "scstep" and calculation is not None:
                calculation["scstep_count"] += 1
                calculation["scsteps"].append(current_scstep)
                current_scstep = None
            elif tag == "calculation":
                calculation["complete"] = True
                calculation = None
                spin_index = kpoint_index = None
            elif tag == "modeling":
                closed_root = True
            element.clear()
            elements.pop()
            if elements:
                elements[-1].remove(element)
            stack.pop()

    parse_error = None
    with reader:
        for line in reader:
            if parse_error is not None:
                continue
            try:
                if "<!DOCTYPE" in line.text.upper() or "<!ENTITY" in line.text.upper():
                    raise ValueError("XML declarations/entities are unsupported")
                parser.feed(line.text)
                consume_events()
            except (ET.ParseError, ValueError, IndexError) as exc:
                parse_error = str(exc)
        if parse_error is None:
            try:
                parser.close()
                consume_events()
            except (ET.ParseError, ValueError, IndexError) as exc:
                parse_error = str(exc)
    result["source"] = reader.record
    result["complete"] = closed_root and parse_error is None and reader.record["coverage"] == "complete"
    if parse_error is not None:
        result["findings"].append({"code": "xml.incomplete_or_invalid", "message": parse_error})
    if len(coordinates) != len(weights):
        result["findings"].append({"code": "xml.kpoint_weight_count"})
    else:
        result["kpoints"] = [{"fractional": coordinate, "weight": weight, "fractional_tokens": tokens}
                             for coordinate, weight, tokens in zip(coordinates, weights, coordinate_tokens)]
    calculations = result["calculations"]
    selected_index = evaluation_index if evaluation_index is not None else len(calculations) - 1
    if 0 <= selected_index < len(calculations):
        selected = calculations[selected_index]
        result["selected_calculation"] = selected
        spectrum = selected["spectrum"]
        if spectrum is not None:
            spectrum.update(source=reader.record, settings=result["settings"],
                            kpoints=result["kpoints"], nelect=result["settings"].get("NELECT"),
                            nspin=result["settings"].get("ISPIN"), nkpoints=len(coordinates),
                            nbands=result["settings"].get("NBANDS"),
                            complete=result["complete"] and selected["complete"],
                            evaluation_index=selected_index)
            spectrum["findings"].extend(result["findings"])
            result["spectrum"] = spectrum
    elif evaluation_index is not None:
        result["findings"].append({"code": "xml.evaluation_unavailable"})
    return result


def read_eigenval(path: str | Path) -> dict:
    """Read the ordinary ISPIN=1/2 EIGENVAL grammar without geometry inference."""
    reader = SnapshotReader(path, "EIGENVAL")
    result = {"format": "eigenval", "settings": {}, "kpoints": [], "eigenvalues": [],
              "findings": [], "complete": False, "nspin": None, "nkpoints": None,
              "nbands": None, "nelect": None}
    error = None
    header = []
    remaining_bands = 0
    with reader:
        for line in reader:
            if error is not None:
                continue
            try:
                if not line.text.endswith("\n"):
                    raise ValueError("partial trailing EIGENVAL record")
                tokens = line.text.split()
                if len(header) < 6:
                    header.append(tokens)
                    if len(header) == 1:
                        nspin = int(tokens[3])
                        if nspin not in (1, 2):
                            raise ValueError("unsupported EIGENVAL spin representation")
                        result["nspin"] = nspin
                        result["eigenvalues"] = [[] for _ in range(nspin)]
                    elif len(header) == 6:
                        if len(tokens) != 3:
                            raise ValueError("invalid EIGENVAL counts")
                        nelect, nkpoints, nbands = _numbers(line.text, 3)
                        if nelect < 0 or nkpoints <= 0 or nbands <= 0 or int(nkpoints) != nkpoints or int(nbands) != nbands:
                            raise ValueError("invalid EIGENVAL dimensions")
                        result.update(nelect=nelect, nkpoints=int(nkpoints), nbands=int(nbands))
                    continue
                if not tokens:
                    if remaining_bands:
                        raise ValueError("missing EIGENVAL band rows")
                    continue
                if remaining_bands == 0:
                    if len(result["kpoints"]) >= result["nkpoints"]:
                        raise ValueError("trailing EIGENVAL data")
                    coordinate = _numbers(line.text, 4)
                    result["kpoints"].append({"fractional": coordinate[:3], "weight": coordinate[3],
                                              "fractional_tokens": [number(token) for token in tokens[:3]]})
                    for channel in result["eigenvalues"]:
                        channel.append([])
                    remaining_bands = result["nbands"]
                else:
                    nspin = result["nspin"]
                    if len(tokens) != 1 + 2 * nspin:
                        raise ValueError("unsupported EIGENVAL band columns")
                    label = int(tokens[0])
                    if label != result["nbands"] - remaining_bands + 1:
                        raise ValueError("nonsequential EIGENVAL band labels")
                    for spin in range(nspin):
                        result["eigenvalues"][spin][-1].append({
                            "energy": number(tokens[1 + spin]),
                            "occupation": number(tokens[1 + nspin + spin]),
                            "native_band_label": label})
                    remaining_bands -= 1
            except (ValueError, IndexError, TypeError) as exc:
                error = str(exc)
    result["source"] = reader.record
    result["complete"] = (error is None and len(header) == 6 and remaining_bands == 0
                          and len(result["kpoints"]) == result["nkpoints"]
                          and reader.record["coverage"] == "complete")
    if not result["complete"]:
        result["findings"].append({"code": "eigenval.incomplete_or_invalid", "message": error})
    return result


def analyze_spectrum(spectrum: dict, settings: dict, *, occupation_tolerance: float = 1e-3,
                     energy_tolerance_ev: float = 1e-5, all_points: bool = False) -> dict:
    """Classify sampled extrema using occupations, never a guessed Fermi energy.

    Normalized occupation one is full. VASP XML/EIGENVAL print one for both
    restricted and collinear full occupations; OUTCAR uses two when ISPIN=1.
    Electron counting still includes the restricted spin degeneracy of two.
    """
    if not math.isfinite(occupation_tolerance) or not 0 < occupation_tolerance < 0.5:
        raise ValueError("occupation_tolerance must be finite and between zero and 0.5")
    if not math.isfinite(energy_tolerance_ev) or energy_tolerance_ev < 0:
        raise ValueError("energy_tolerance_ev must be finite and nonnegative")
    settings = {key: (value.get("value") if isinstance(value, dict) else value)
                for key, value in settings.items()}
    findings = list(spectrum.get("findings", []))
    result = {"status": "UNKNOWN", "classification": "insufficient", "vbm": None,
              "cbm": None, "gap": None, "per_spin": [], "findings": findings,
              "scope": "all-sampled-points" if all_points else "positive-weight-integration-points",
              "excluded_zero_weight_indices": [], "occupation_tolerance": occupation_tolerance,
              "energy_tolerance_ev": energy_tolerance_ev, "electron_count": None,
              "qualifications": ["sampled extrema only; not a proof of the continuous Brillouin-zone extrema",
                                 "no Fermi energy is inferred from occupations or band edges"]}
    if any(settings.get(tag) is True for tag in ("LSORBIT", "LNONCOLLINEAR")):
        result.update(status="UNSUPPORTED", classification="unsupported")
        findings.append({"code": "spectrum.spin_representation"})
        return result
    nspin = spectrum.get("nspin") or settings.get("ISPIN")
    if nspin not in (1, 2) or (settings.get("ISPIN") is not None and settings["ISPIN"] != nspin):
        result["status"] = "UNSUPPORTED"
        findings.append({"code": "spectrum.spin_count"})
        return result
    if settings.get("ISMEAR") not in (-5, -1, 0):
        result["status"] = "UNSUPPORTED"
        findings.append({"code": "spectrum.smearing_unsupported", "ISMEAR": settings.get("ISMEAR")})
        return result
    normalization = 2.0 if spectrum.get("format") == "outcar" and nspin == 1 else 1.0
    result["occupation_normalization"] = {"raw_full_occupation": normalization,
                                           "spin_degeneracy": 2 if nspin == 1 else 1}
    kpoints, eigenvalues = spectrum.get("kpoints", []), spectrum.get("eigenvalues", [])
    try:
        if not kpoints or len(eigenvalues) != nspin:
            raise ValueError("missing kpoints or spin channels")
        coordinates = finite_array([point["fractional"] for point in kpoints], ndim=2, name="k coordinates")
        weights = finite_array([point["weight"] for point in kpoints], ndim=1, name="k weights")
        if coordinates.shape != (len(kpoints), 3) or any(weights < 0):
            raise ValueError("invalid kpoint coordinates or negative weights")
        if any(len(channel) != len(kpoints) for channel in eigenvalues):
            raise ValueError("kpoint count differs between spin channels")
        band_counts = {len(bands) for channel in eigenvalues for bands in channel}
        if len(band_counts) != 1 or 0 in band_counts:
            raise ValueError("missing or unequal band counts")
        nbands = next(iter(band_counts))
        if spectrum.get("nbands") is not None and spectrum["nbands"] != nbands:
            raise ValueError("band count differs from native dimensions")
        if spectrum.get("nkpoints") is not None and spectrum["nkpoints"] != len(kpoints):
            raise ValueError("kpoint count differs from native dimensions")
        for channel in eigenvalues:
            for bands in channel:
                finite_array([row["energy"]["value"] for row in bands], ndim=1, name="band energies")
                finite_array([row["occupation"]["value"] for row in bands], ndim=1, name="occupations")
                if [row["native_band_label"] for row in bands] != list(range(1, nbands + 1)):
                    raise ValueError("nonsequential band identities")
                if any(row["occupation"]["value"] / normalization < -occupation_tolerance
                       or row["occupation"]["value"] / normalization > 1 + occupation_tolerance for row in bands):
                    raise ValueError("occupation outside supported normalized interval")
    except (ValueError, KeyError, TypeError) as exc:
        result["status"] = "FAIL"
        findings.append({"code": "spectrum.invalid", "message": str(exc)})
        return result
    zero_indices = [i for i, weight in enumerate(weights) if weight == 0]
    result["excluded_zero_weight_indices"] = [] if all_points else zero_indices
    selected = list(range(len(kpoints))) if all_points else [i for i, weight in enumerate(weights) if weight > 0]
    positive = [i for i, weight in enumerate(weights) if weight > 0]
    if not selected or not positive:
        findings.append({"code": "spectrum.no_integration_points"})
        return result
    weight_sum = float(sum(weights[i] for i in positive))
    if abs(weight_sum - 1) > 1e-5:
        result["status"] = "FAIL"
        findings.append({"code": "spectrum.weight_sum", "value": weight_sum})
        return result
    # Normalize the printed integration weights only within their 1e-5 sum bound.
    counted = sum(float(weights[k]) * row["occupation"]["value"] / normalization
                  for channel in eigenvalues for k in positive for row in channel[k])
    counted *= (2 if nspin == 1 else 1) / weight_sum
    expected = settings.get("NELECT", spectrum.get("nelect"))
    count_bound = max(1e-5, occupation_tolerance * (2 if nspin == 1 else 1))
    count_ok = isinstance(expected, (int, float)) and math.isfinite(expected) and abs(counted - expected) <= count_bound
    result["electron_count"] = {"observed": counted, "expected": expected, "tolerance": count_bound,
                                "status": "PASS" if count_ok else "UNKNOWN" if expected is None else "FAIL"}
    if not count_ok:
        findings.append({"code": "spectrum.electron_count", **result["electron_count"]})

    def edge(spin, point, band):
        row = eigenvalues[spin][point][band]
        return {"value": row["energy"]["value"], "units": "eV", "spin_index": spin,
                "kpoint_index": point, "band_index": band, "native_spin_label": spin + 1,
                "native_kpoint_label": point + 1, "native_band_label": row["native_band_label"],
                "k_fractional": list(kpoints[point]["fractional"]), "energy": row["energy"],
                "raw_occupation": row["occupation"],
                "normalized_occupation": row["occupation"]["value"] / normalization}

    occupation_boundary_ambiguity = False

    def attach_ties(best, states):
        if best is None:
            return
        best["precision_tied_states"] = [
            {key: state[key] for key in ("spin_index", "kpoint_index", "band_index", "native_band_label", "k_fractional")}
            for state in states if abs(state["value"] - best["value"]) <= (
                state["energy"]["half_last_place"] + best["energy"]["half_last_place"] + energy_tolerance_ev)]

    for spin in range(nspin):
        occupied, empty, intermediate = [], [], []
        band_classes = [set() for _ in range(nbands)]
        for point in selected:
            for band, row in enumerate(eigenvalues[spin][point]):
                occupation = row["occupation"]["value"] / normalization
                occupation_error = row["occupation"]["half_last_place"] / normalization
                if any(occupation - occupation_error < boundary < occupation + occupation_error
                       for boundary in (occupation_tolerance, 1 - occupation_tolerance)):
                    occupation_boundary_ambiguity = True
                state = edge(spin, point, band)
                if occupation >= 1 - occupation_tolerance:
                    occupied.append(state)
                    band_classes[band].add("occupied")
                elif occupation <= occupation_tolerance:
                    empty.append(state)
                    band_classes[band].add("empty")
                else:
                    intermediate.append(state)
        vbm = max(occupied, key=lambda item: item["value"]) if occupied else None
        cbm = min(empty, key=lambda item: item["value"]) if empty else None
        raw_gap = cbm["value"] - vbm["value"] if vbm and cbm else None
        if raw_gap is not None and not math.isfinite(raw_gap):
            result["status"] = "FAIL"
            findings.append({"code": "spectrum.nonfinite_derived_separation"})
            return result
        gap_error = vbm["energy"]["half_last_place"] + cbm["energy"]["half_last_place"] if vbm and cbm else 0
        attach_ties(vbm, occupied)
        attach_ties(cbm, empty)
        crossing = any(len(classes) > 1 for classes in band_classes)
        if intermediate or crossing or (raw_gap is not None and raw_gap < -energy_tolerance_ev):
            classification = "metallic"
            gap = 0.0
        elif raw_gap is None:
            classification = "insufficient"
            gap = None
        elif raw_gap - gap_error <= energy_tolerance_ev:
            classification = "unresolved-semimetal"
            gap = None
        else:
            classification = "semiconductor"
            gap = raw_gap
        result["per_spin"].append({"spin_index": spin, "classification": classification,
                                   "vbm": vbm, "cbm": cbm, "gap": gap,
                                   "sampled_extrema_separation_ev": raw_gap,
                                   "intermediate_occupation_count": len(intermediate),
                                   "crossing_band_indices": [i for i, classes in enumerate(band_classes) if len(classes) > 1]})
    classes = [channel["classification"] for channel in result["per_spin"]]
    if "insufficient" in classes:
        classification = "insufficient"
    elif "metallic" in classes:
        classification = "half-metallic" if nspin == 2 and "semiconductor" in classes else "metallic"
    elif "unresolved-semimetal" in classes:
        classification = "unresolved-semimetal"
    else:
        classification = "semiconductor"
    available_vbm = [channel["vbm"] for channel in result["per_spin"] if channel["vbm"] is not None]
    available_cbm = [channel["cbm"] for channel in result["per_spin"] if channel["cbm"] is not None]
    result["vbm"] = dict(max(available_vbm, key=lambda item: item["value"])) if available_vbm else None
    result["cbm"] = dict(min(available_cbm, key=lambda item: item["value"])) if available_cbm else None
    for role, candidates in (("vbm", available_vbm), ("cbm", available_cbm)):
        best = result[role]
        if best is not None:
            best["precision_tied_states"] = [state for candidate in candidates
                if abs(candidate["value"] - best["value"]) <= (
                    candidate["energy"]["half_last_place"] + best["energy"]["half_last_place"] + energy_tolerance_ev)
                for state in candidate["precision_tied_states"]]
    global_separation = (result["cbm"]["value"] - result["vbm"]["value"]
                         if result["vbm"] is not None and result["cbm"] is not None else None)
    if global_separation is not None and not math.isfinite(global_separation):
        result["status"] = "FAIL"
        findings.append({"code": "spectrum.nonfinite_derived_separation"})
        return result
    result["sampled_extrema_separation_ev"] = global_separation
    if classification == "semiconductor" and global_separation is not None:
        if global_separation < -energy_tolerance_ev:
            classification = "metallic"
        elif global_separation <= energy_tolerance_ev:
            classification = "unresolved-semimetal"
    result["classification"] = classification
    result["gap"] = (global_separation if classification == "semiconductor" else
                     0.0 if classification in {"metallic", "half-metallic"} else None)
    if result["vbm"] is not None and result["cbm"] is not None:
        result["gap_half_last_place_ev"] = (result["vbm"]["energy"]["half_last_place"]
                                             + result["cbm"]["energy"]["half_last_place"])
        if classification == "semiconductor" and global_separation - result["gap_half_last_place_ev"] <= energy_tolerance_ev:
            result.update(classification="unresolved-semimetal", gap=None)
            classification = "unresolved-semimetal"
    if occupation_boundary_ambiguity:
        findings.append({"code": "spectrum.occupation_precision_boundary"})
        result.update(classification="ambiguous-occupations", gap=None)
        classification = "ambiguous-occupations"
    if classification in {"metallic", "half-metallic"}:
        findings.append({"code": "spectrum.metallic_occupations_or_crossing"})
        result["qualifications"].append("occupied/empty extrema in metallic spectra are diagnostic, not semiconductor VBM/CBM")
    if not count_ok:
        result["status"] = "UNKNOWN" if expected is None else "FAIL"
    elif not spectrum.get("complete"):
        result["status"] = "UNKNOWN"
    elif findings:
        result["status"] = "UNKNOWN"
    elif classification in {"insufficient", "unresolved-semimetal", "ambiguous-occupations"}:
        result["status"] = "UNKNOWN"
    else:
        result["status"] = "PASS"
    return result
