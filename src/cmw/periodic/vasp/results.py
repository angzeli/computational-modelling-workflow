"""Streaming native VASP evidence; no acceptance thresholds or execution actions.

Dialect: VASP 6.6.1 ordinary SCF Iteration i(j), DAV/RMM OSZICAR tables,
POSITION/TOTAL-FORCE and FREE ENERGIE blocks. Unknown modes stay diagnostic.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re

import numpy as np
from ase.geometry import find_mic

from cmw.core.provenance import stable_hash
from .inputs import parse_incar, parse_poscar
from .result_sources import SnapshotReader, SourceSnapshotError

PARSER_VERSION = "1"
NUMBER = r"[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[EeDd][+-]?\d+)?|NaN|Inf(?:inity)?|\*+)"
NUM = re.compile(NUMBER, re.I)
ITERATION = re.compile(r"\bIteration\s+(\d+)\(\s*(\d+)\)")
BANNER = re.compile(r"^\s*vasp\.(\d+\.\d+\.\d+)\b", re.I)
SETTINGS = set("EDIFF NELM NELMIN NELMDL EDIFFG IBRION ISIF NSW ISPIN IALGO ICHARG ISTART ENCUT ISMEAR SIGMA NELECT NCORE KPAR NPAR GGA METAGGA LHFCALC LSORBIT LNONCOLLINEAR ML_LMLFF ML_MODE IMAGES ICHAIN I_CONSTRAINED_M EFIELD EFIELD_PEAD LPEAD LEPSILON LCALCEPS LOPTICS LCHIMAG LORBMOM LATTICE_CONSTRAINTS EFIRST EFOR IVDW LDAU LDIPOL IDIPOL ALGO".split())
SET_RE = re.compile(r"\b(" + "|".join(sorted(SETTINGS, key=len, reverse=True)) + r")\s*=\s*([^\s;]+)")
POSITION_TOLERANCE = 2e-5  # Angstrom; OUTCAR position components printed to 5 decimals.
CELL_TOLERANCE = 2e-6      # Angstrom; observed lattice components printed to 7 decimals.


def reference(line, role="OUTCAR"):
    return {"role": role, "line_start": line.line_number, "line_end": line.line_number,
            "byte_start": line.byte_start, "byte_end": line.byte_end}


def number(token):
    """Keep printed resolution: half a last-place unit, not extra scientific tolerance."""
    try:
        value = Decimal(token.replace("D", "E").replace("d", "e"))
        if not value.is_finite() or not math.isfinite(float(value)):
            raise InvalidOperation
        resolution = Decimal(1).scaleb(value.as_tuple().exponent)
        if not math.isfinite(float(resolution)) or float(resolution) == 0:
            raise InvalidOperation
        return {"value": float(value), "printed": token,
                "half_last_place": float(abs(resolution) / 2)}
    except (InvalidOperation, ValueError, OverflowError):
        return {"value": None, "printed": token, "half_last_place": None,
                "finding": "nonfinite_or_overflow"}


def below_printed(value, threshold):
    if value is None or value.get("value") is None or threshold is None:
        return "UNKNOWN"
    if threshold <= 0:
        return "NOT_APPLICABLE"
    magnitude, error = abs(value["value"]), value["half_last_place"]
    if magnitude + error < threshold:
        return "PASS"
    if max(0, magnitude - error) >= threshold:
        return "FAIL"
    return "UNKNOWN"


def _scalar(token):
    token = token.strip().upper()
    if token in {"T", ".TRUE.", "TRUE"}:
        return True
    if token in {"F", ".FALSE.", "FALSE"}:
        return False
    if re.fullmatch(r"[+-]?\d+", token):
        return int(token)
    parsed = number(token)
    return parsed["value"] if parsed["value"] is not None else token


def _segment(index, line, version=None):
    return {"index": index, "version": version, "range": reference(line),
            "settings": {}, "echoed_incar": {}, "evaluations": [], "cell": None, "cell_history": [],
            "initial_positions": None, "species": [], "counts": [], "nions": None,
            "termination": {"normal_footer": False, "footer_reference": None,
                            "timing": {}, "fatal": [], "requested_stop": []},
            "ionic_stop": None, "conflicts": [], "unsupported": [],
            "endpoint": None, "trailing_incomplete": False}


def _evaluation(index, ionic, line):
    return {"index": index, "native_ionic_label": ionic, "reference": reference(line),
            "iterations": [], "native_convergence": None, "native_convergence_reference": None,
            "complete": False, "force_block_complete": False, "energies": {},
            "accepted_optimizer_step": None, "trial_messages": [], "force_summary": None}


def _force_summary(vectors, flags=None):
    norms = [math.hypot(*v) for v in vectors]
    def group(indices):
        if not indices:
            return {"count": 0, "atom_indices": [], "maximum_norm": None, "maximum_atom_index": None}
        maximum = max(indices, key=lambda i: norms[i])
        return {"count": len(indices), "atom_indices": indices, "maximum_norm": norms[maximum],
                "maximum_atom_index": maximum}
    flags = flags if flags is not None else [[True] * 3 for _ in vectors]
    return {"units": "eV/Angstrom", "index_base": 0, "all": group(list(range(len(vectors)))),
            "free": group([i for i, f in enumerate(flags) if all(f)]),
            "fixed": group([i for i, f in enumerate(flags) if not any(f)]),
            "partial": [i for i, f in enumerate(flags) if any(f) and not all(f)]}


def parse_outcar(path, *, max_bytes=None):
    segments, current, evaluation = [], None, None
    block, rows, block_ref = None, [], None
    pending_geometry = None
    in_echo, in_final_energy = False, False
    force_end_pending = False
    with SnapshotReader(path, "OUTCAR", max_bytes=max_bytes) as reader:
        for line in reader:
            text = line.text
            if force_end_pending and text.strip():
                if set(text.strip()) != {"-"}:
                    current["conflicts"].append({"code": "force_block_terminator", "reference": reference(line)})
                force_end_pending = False
            banner = BANNER.match(text)
            if banner or current is None:
                if current is not None:
                    if evaluation is not None and not evaluation["complete"]:
                        current["trailing_incomplete"] = True
                    segments.append(current)
                current = _segment(len(segments), line, banner[1] if banner else None)
                evaluation, block, rows, pending_geometry = None, None, [], None
                in_echo, in_final_energy = False, False
            current["range"].update(line_end=line.line_number, byte_end=line.byte_end)
            if "INCAR:" in text:
                in_echo = True
            if "POTCAR:" in text:
                in_echo = False
            # Effective settings are read only before the electronic loop. The raw
            # INCAR echo is retained separately; later filesystem INCAR is never substituted.
            if evaluation is None:
                for match in SET_RE.finditer(text):
                    target = current["echoed_incar"] if in_echo else current["settings"]
                    key, value = match[1].upper(), _scalar(match[2])
                    if key in target and target[key]["value"] != value:
                        current["conflicts"].append({"code": "conflicting_setting", "tag": key,
                                                     "previous": target[key], "reference": reference(line)})
                    target[key] = {"value": value, "reference": reference(line)}
            species = re.search(r"VRHFIN\s*=\s*([A-Z][a-z]?)\s*:", text)
            if species and evaluation is None:
                current["species"].append(species[1])
            count = re.search(r"\bNIONS\s*=\s*(\d+)", text)
            if count:
                current["nions"] = int(count[1])
            if "ions per type =" in text:
                current["counts"] = [int(v) for v in text.split("=", 1)[1].split()]
            iteration = ITERATION.search(text)
            if iteration:
                if current["termination"]["footer_reference"]:
                    current["conflicts"].append({"code": "evaluation_after_footer", "reference": reference(line)})
                ionic, electronic = map(int, iteration.groups())
                if evaluation is None or electronic == 1 or ionic != evaluation["native_ionic_label"]:
                    if evaluation is not None and not evaluation["complete"]:
                        current["trailing_incomplete"] = True
                    evaluation = _evaluation(len(current["evaluations"]), ionic, line)
                    if electronic != 1:
                        current["conflicts"].append({"code": "missing_electronic_prefix", "reference": reference(line)})
                    current["evaluations"].append(evaluation)
                elif electronic != evaluation["iterations"][-1]["label"] + 1:
                    current["conflicts"].append({"code": "electronic_label_gap", "reference": reference(line)})
                evaluation["iterations"].append({"label": electronic, "reference": reference(line),
                                                 "algorithm": None, "electronic_free_energy": None})
                in_final_energy, pending_geometry, block = False, None, None
            if evaluation is not None:
                if "EDDAV:" in text:
                    evaluation["iterations"][-1]["algorithm"] = "DAV"
                if re.search(r"\b(?:RMM-DIIS|RMM:)", text):
                    evaluation["iterations"][-1]["algorithm"] = "RMM"
                if "aborting loop because EDIFF is reached" in text:
                    evaluation["native_convergence"] = True
                    evaluation["native_convergence_reference"] = reference(line)
                elif "EDIFF was not reached" in text:
                    evaluation["native_convergence"] = False
                    evaluation["native_convergence_reference"] = reference(line)
                if re.search(r"\b(?:trial:|trial-energy|ZBRENT:|curvature:)\b", text):
                    evaluation["trial_messages"].append(reference(line))
            if "direct lattice vectors" in text:
                block, rows, block_ref = "cell", [], reference(line)
                continue
            if "position of ions in cartesian coordinates" in text and evaluation is None:
                block, rows, block_ref = "initial", [], reference(line)
                continue
            if "POSITION" in text and "TOTAL-FORCE" in text:
                block, rows, block_ref = "force", [], reference(line)
                if evaluation is None:
                    current["conflicts"].append({"code": "unassociated_force_block", "reference": reference(line)})
                continue
            if block:
                tokens = text.split()
                width = 6 if block in {"force", "cell"} else 3
                valid_row = len(tokens) >= width and all(re.fullmatch(NUMBER, t, re.I) for t in tokens[:width])
                if valid_row:
                    parsed = [number(t) for t in tokens[:width]]
                    rows.append(parsed)
                    expected = 3 if block == "cell" else current["nions"]
                    if expected is not None and len(rows) == expected:
                        block_ref.update(line_end=line.line_number, byte_end=line.byte_end)
                        values = [[v["value"] for v in row] for row in rows]
                        if any(v is None for row in values for v in row):
                            current["conflicts"].append({"code": "nonfinite_geometry_or_force", "reference": block_ref})
                        elif block == "cell":
                            current["cell"] = {"matrix": [r[:3] for r in values], "reference": block_ref}
                            current["cell_history"].append(current["cell"])
                        elif block == "initial":
                            current["initial_positions"] = {"cartesian": values, "reference": block_ref, "cell": current["cell"]}
                        elif evaluation is not None:
                            pending_geometry = {"cartesian": [r[:3] for r in values],
                                                "forces": [r[3:] for r in values],
                                                "force_half_last_place": max(v["half_last_place"] for r in rows for v in r[3:]),
                                                "reference": block_ref, "evaluation_index": evaluation["index"],
                                                "cell": current["cell"]}
                            force_end_pending = True
                            evaluation["force_block_complete"] = True
                            evaluation["force_summary"] = _force_summary(pending_geometry["forces"])
                        block, rows = None, []
                        continue
                    continue
                if not text.strip() or set(text.strip()) == {"-"}:
                    continue
                if rows:
                    if block == "initial" and current["nions"] is None:
                        current["initial_positions"] = {"cartesian": [[v["value"] for v in r] for r in rows], "reference": block_ref, "cell": current["cell"]}
                    else:
                        current["conflicts"].append({"code": "incomplete_" + block + "_block", "reference": block_ref})
                block, rows = None, []
            if "FREE ENERGIE OF THE ION-ELECTRON SYSTEM" in text:
                in_final_energy = True
            toten = re.search(r"free\s+energy\s+TOTEN\s*=\s*(" + NUMBER + ")", text, re.I)
            if toten and evaluation is not None:
                record = {**number(toten[1]), "units": "eV", "reference": reference(line)}
                if in_final_energy:
                    evaluation["energies"]["free_energy"] = record
                else:
                    evaluation["iterations"][-1]["electronic_free_energy"] = record
            energies = re.search(r"energy\s+without entropy\s*=\s*(" + NUMBER + r").*energy\(sigma->0\)\s*=\s*(" + NUMBER + ")", text, re.I)
            if energies and evaluation is not None and in_final_energy:
                for name, token in zip(("without_entropy", "sigma_to_zero"), energies.groups()):
                    evaluation["energies"][name] = {**number(token), "units": "eV", "reference": reference(line)}
                if pending_geometry and evaluation["energies"].get("free_energy", {}).get("value") is not None:
                    evaluation["complete"] = all(v["value"] is not None for v in evaluation["energies"].values())
                    if evaluation["complete"]:
                        current["endpoint"] = pending_geometry
                in_final_energy = False
            if "reached required accuracy - stopping structural energy minimisation" in text:
                current["ionic_stop"] = reference(line)
            if "General timing and accounting informations for this job:" in text:
                current["termination"]["footer_reference"] = reference(line)
            timing = re.search(r"(Total CPU time used|User time|System time|Elapsed time)\s*\(sec\):\s*(" + NUMBER + ")", text)
            if timing and current["termination"]["footer_reference"]:
                current["termination"]["timing"][timing[1]] = {**number(timing[2]), "reference": reference(line)}
            if re.search(r"VERY BAD NEWS|internal error|ERROR FEXCP|ZHEGV failed|ERROR EDD|killed by signal", text, re.I):
                current["termination"]["fatal"].append(reference(line))
            if re.search(r"LSTOP\s*=\s*T|LABORT\s*=\s*T|STOPCAR.*(?:stop|found)|soft stop", text, re.I):
                current["termination"]["requested_stop"].append(reference(line))
    if current is not None:
        if evaluation is not None and not evaluation["complete"]:
            current["trailing_incomplete"] = True
        if force_end_pending or block in {"force", "cell"}:
            current["trailing_incomplete"] = True
        segments.append(current)
    for segment in segments:
        termination = segment["termination"]
        termination["normal_footer"] = len(termination["timing"]) == 4 and all(x["value"] is not None for x in termination["timing"].values())
        segment["segment_id"] = stable_hash({"source_sha256": reader.record["sha256"], "range": segment["range"], "index": segment["index"]})
    return segments, reader.record


def parse_oszicar(path, role="OSZICAR", *, max_bytes=None):
    """Keep chronological evaluation groups, including unfinished trailing groups."""
    segments, evaluations, rows = [], [], []
    with SnapshotReader(path, role, max_bytes=max_bytes) as reader:
        for line in reader:
            text = line.text
            electronic = re.match(r"\s*([A-Z][A-Z0-9 -]*?)\s*:\s*(\d+)\s+(.*)", text)
            if electronic and electronic[1].strip() not in {"DAV", "RMM", "CG", "DMP"} and len(NUM.findall(electronic[3])) < 5:
                electronic = None
            if electronic:
                label = int(electronic[2])
                if label == 1 and rows:
                    evaluations.append({"rows": rows, "summary": None})
                    rows = []
                tokens = NUM.findall(electronic[3])
                # Full token coverage permits adjacent signed Fortran fields but
                # rejects omitted columns and unknown trailing text.
                residue = NUM.sub("", electronic[3]).strip()
                row = {"label": label, "algorithm": electronic[1].strip(), "reference": reference(line, role)}
                if len(tokens) not in (5, 6) or residue:
                    row["malformed"] = True
                else:
                    for key, token in zip(("energy", "dE", "d_eps", "ncg", "rms", "rms_c"), tokens):
                        row[key] = number(token)
                rows.append(row)
            summary = re.match(r"\s*(\d+)\s+F=\s*(" + NUMBER + r")\s+E0=\s*(" + NUMBER + r")\s+d\s*E\s*=\s*(" + NUMBER + ")", text, re.I)
            if summary:
                native = int(summary[1])
                if evaluations and evaluations[-1]["summary"] is not None and native <= evaluations[-1]["summary"]["native_ionic_label"]:
                    segments.append(evaluations)
                    evaluations = []
                evaluations.append({"rows": rows, "summary": {"native_ionic_label": native,
                                    "free_energy": number(summary[2]), "sigma_to_zero": number(summary[3]),
                                    "ionic_dE": number(summary[4]), "reference": reference(line, role)}})
                rows = []
    if rows:
        evaluations.append({"rows": rows, "summary": None})
    if evaluations:
        segments.append(evaluations)
    return segments, reader.record


def _same_printed(first, second):
    if not first or not second or first.get("value") is None or second.get("value") is None:
        return False
    return abs(first["value"] - second["value"]) <= first["half_last_place"] + second["half_last_place"] + 1e-12


def _associate(segment, summaries):
    evaluations = segment["evaluations"]
    if len(evaluations) != len(summaries):
        return False
    for evaluation, summary in zip(evaluations, summaries):
        native = evaluation["iterations"]
        rows = summary["rows"]
        if [r["label"] for r in native] != [r["label"] for r in rows] or not rows:
            return False
        if summary["summary"] is not None:
            if summary["summary"]["native_ionic_label"] != evaluation["native_ionic_label"]:
                return False
            if not _same_printed(evaluation["energies"].get("free_energy"), summary["summary"]["free_energy"]) or not _same_printed(evaluation["energies"].get("sigma_to_zero"), summary["summary"]["sigma_to_zero"]):
                return False
        for left, right in zip(native, rows):
            if right.get("malformed") or not _same_printed(left["electronic_free_energy"], right.get("energy")):
                return False
    for evaluation, summary in zip(evaluations, summaries):
        for iteration, row in zip(evaluation["iterations"], summary["rows"]):
            iteration["oszicar"] = row
        evaluation["oszicar_summary"] = summary["summary"]
    return True


def compare_geometry(first, second, *, position_tolerance=POSITION_TOLERANCE, cell_tolerance=CELL_TOLERANCE):
    """Ordered PBC comparison using ASE's general minimum image (skew cells too)."""
    result = {"status": "unavailable", "position_tolerance_angstrom": position_tolerance,
              "cell_tolerance_angstrom": cell_tolerance, "metric": "ASE general minimum image; fixed order",
              "max_displacement_angstrom": None, "max_cell_difference_angstrom": None}
    if first is None or second is None:
        return result
    if first["species"] != second["species"] or len(first["cartesian"]) != len(second["cartesian"]):
        return dict(result, status="mismatch", reason="ordered species/count mismatch")
    try:
        cell = np.asarray(first["cell"], float)
        difference = float(np.max(np.abs(cell - np.asarray(second["cell"], float))))
        if not math.isfinite(float(np.linalg.det(cell))) or np.linalg.det(cell) <= 0:
            return dict(result, status="unsupported", reason="nonpositive or singular cell")
        _, distances = find_mic(np.asarray(first["cartesian"]) - np.asarray(second["cartesian"]), cell, pbc=True)
        maximum = float(np.max(distances))
        return dict(result, status="match" if difference <= cell_tolerance and maximum <= position_tolerance else "mismatch",
                    max_displacement_angstrom=maximum, max_cell_difference_angstrom=difference)
    except (ValueError, np.linalg.LinAlgError):
        return dict(result, status="unsupported", reason="invalid cell or coordinates")


def _parse_result_structure(raw):
    """Reuse POSCAR semantics; permit only the documented all-zero velocity tail.

    VASP writes this tail in static/relaxation CONTCAR too. It has no role in
    the supported non-MD evidence. Nonzero velocities/other tails stay unsupported.
    """
    parsed = parse_poscar(raw)
    suffixes = [f for f in parsed["findings"] if f["code"] == "poscar.coordinate_suffix"]
    if suffixes and "atom_species" in parsed:
        lines = raw.splitlines()
        start = 9 if parsed["selective_dynamics"] is not None else 8
        width = 6 if parsed["selective_dynamics"] is not None else 3
        # Only one exact elemental row label is covered; arbitrary extra fields
        # remain unsupported. Native species identity still comes from OUTCAR.
        if all(lines[start+i].split()[width:] in ([], [element]) for i, element in enumerate(parsed["atom_species"])):
            for i in range(parsed["atom_count"]):
                lines[start+i] = " ".join(lines[start+i].split()[:width])
            parsed = parse_poscar("\n".join(lines) + "\n")
            parsed["row_label_annotations"] = "matching explicit elemental suffixes"
    unsupported = [f for f in parsed["findings"] if f["severity"] == "unsupported"]
    if parsed["status"] == "unsupported" and len(unsupported) == 1 and unsupported[0]["code"] == "poscar.extra_sections":
        start = unsupported[0]["evidence"]["first_line"] - 1
        tail = raw.splitlines()[start:]
        nonempty = [line.split() for line in tail if line.strip()]
        if tail and not tail[0].strip() and len(nonempty) == parsed["atom_count"] and all(len(row) == 3 and all(re.fullmatch(NUMBER, t, re.I) and number(t)["value"] == 0 for t in row) for row in nonempty):
            parsed["findings"].remove(unsupported[0])
            parsed["status"] = "valid"
            parsed["exit_code"] = 0
            parsed["unused_tail"] = "all-zero Cartesian velocities; non-MD only"
    return parsed


def _geometry(parsed):
    if not parsed or parsed.get("status") != "valid":
        return None
    return {"cell": parsed["cell"], "species": parsed["atom_species"],
            "cartesian": parsed["cartesian_coordinates"], "constraints": parsed["selective_dynamics"]}


def _read_small(path, role, sources, *, limit=4 * 1024 * 1024):
    with SnapshotReader(path, role, max_bytes=limit) as reader:
        text = "".join(line.text for line in reader)
    sources.append(reader.record)
    if reader.record["coverage"] != "complete":
        raise ValueError(f"Incomplete {role} source")
    return text


def _scope(segment):
    values = {k: v["value"] for k, v in segment["settings"].items()}
    echoed = {k: v["value"] for k, v in segment["echoed_incar"].items()}
    reasons = []
    if segment["version"] != "6.6.1":
        reasons.append("unvalidated_version_dialect")
    for key in ("LHFCALC", "LSORBIT", "LNONCOLLINEAR"):
        if key not in values:
            reasons.append("missing_mode_evidence:" + key)
        elif values[key] is not False:
            reasons.append("unsupported_mode:" + key)
    if values.get("IALGO") not in (38, 48) or values.get("ISPIN") not in (1, 2) or values.get("ICHARG") not in (0, 1, 2):
        reasons.append("unsupported_electronic_mode")
    for key in ("ML_LMLFF", "ML_MODE", "IMAGES", "ICHAIN", "I_CONSTRAINED_M", "EFIELD", "EFIELD_PEAD", "LEPSILON", "LCALCEPS", "LOPTICS", "LCHIMAG", "EFOR", "LATTICE_CONSTRAINTS"):
        v = values.get(key, echoed.get(key))
        if (key in {"EFIELD_PEAD", "EFOR", "LATTICE_CONSTRAINTS"} and (key in values or key in echoed)) or v not in (None, False, 0, "NONE"):
            reasons.append("unsupported_control:" + key)
    if values.get("METAGGA", echoed.get("METAGGA")) not in (None, "NONE", "--"):
        reasons.append("unsupported_meta_gga")
    if values.get("NSW") == 0 and values.get("IBRION") == -1:
        mode = "static"
    elif type(values.get("NSW")) is int and values["NSW"] > 0 and values.get("IBRION") in (1, 2, 3) and values.get("ISIF") == 2:
        mode = "fixed-cell-relaxation"
    else:
        mode = "unsupported"
        reasons.append("unsupported_ionic_mode")
    return {"calculation": mode, "supported": not reasons, "reasons": reasons}


def inspect_result(directory, *, segment=None, stdout=None, max_bytes=None):
    """Read one explicit run directory without writing or querying Jobs state.

    File correspondence is reconstructed from full native labels and printed
    energy intervals, not directory co-location. This is not execution binding.
    """
    root = Path(directory).expanduser().absolute()
    result = {"schema_version": 1, "kind": "vasp-result-evidence", "parser": {"name": "cmw-vasp-native", "version": PARSER_VERSION,
              "dialect": "6.6.1 ordinary DAV/RMM SCF, fixed cell"}, "run_directory": str(root),
              "sources": [], "conflicts": [], "limitations": ["No OS exit code is inferred from native output.",
              "Current input bytes alone do not prove executed input identity.",
              "Cross-file correspondence is numerical/structural evidence, not an atomic snapshot.",
              "Optimizer trial evaluations are not counted as accepted steps.",
              "Wavefunction, charge-density validity and physical-model accuracy are unassessed."],
              "execution": {"exit_code": None, "signal": None, "bound": False},
              "policy_assessment": None, "status": "inspected", "exit_code": 0}
    try:
        segments, source = parse_outcar(root / "OUTCAR", max_bytes=max_bytes)
        result["sources"].append(source)
        result["segments"] = [{"index": s["index"], "segment_id": s["segment_id"], "version": s["version"],
                               "range": s["range"], "evaluation_count": len(s["evaluations"]),
                               "normal_footer": s["termination"]["normal_footer"]} for s in segments]
        if not segments:
            raise ValueError("OUTCAR has no readable segment")
        if segment is None and len(segments) != 1:
            result.update(status="selection_required", exit_code=2, selected_segment=None)
            return _finish_result(result)
        selected = 0 if segment is None else segment
        if type(selected) is not int or not 0 <= selected < len(segments):
            raise ValueError("Selected OUTCAR segment does not exist (zero-based index)")
        current = segments[selected]
        result.update(selected_segment=current, mode=_scope(current))
        result["conflicts"].extend(current["conflicts"])
        table_path = Path(stdout) if stdout else root / "OSZICAR"
        if table_path.exists():
            tables, table_source = parse_oszicar(table_path, "stdout" if stdout else "OSZICAR", max_bytes=max_bytes)
            result["sources"].append(table_source)
            associated = len(tables) == len(segments) and _associate(current, tables[selected])
            result["electronic_table_correspondence"] = "matched" if associated else "conflict"
            if any(row["algorithm"] not in {"DAV", "RMM"} for table in tables for item in table for row in item["rows"]):
                result["mode"]["supported"] = False
                result["mode"]["reasons"].append("unsupported_table_algorithm")
            if not associated:
                result["conflicts"].append({"code": "electronic_table_correspondence", "reason": "native labels, evaluation boundaries, full per-iteration energies or segment counts differ"})
        else:
            result["electronic_table_correspondence"] = "unavailable"
        parsed = {}
        for role in ("POSCAR", "CONTCAR", "INCAR"):
            if (root / role).exists():
                raw = _read_small(root / role, role, result["sources"])
                parsed[role] = parse_incar(raw) if role == "INCAR" else _parse_result_structure(raw)
                parsed[role].pop("raw_text", None)
        result["filesystem_inputs"] = parsed
        values = {k: v["value"] for k, v in current["settings"].items()}
        for evaluation in current["evaluations"]:
            iterations = evaluation["iterations"]
            row = iterations[-1].get("oszicar", {}) if iterations else {}
            if any(i["algorithm"] not in {"DAV", "RMM"} for i in iterations):
                result["mode"]["supported"] = False
                result["mode"]["reasons"].append("unrecognized_native_minimizer_phase")
            threshold = values.get("EDIFF")
            threshold = threshold if type(threshold) in (int, float) else None
            checks = {name: below_printed(row.get(name), threshold) for name in ("dE", "d_eps")}
            numeric = "FAIL" if "FAIL" in checks.values() else "PASS" if all(v == "PASS" for v in checks.values()) else "NOT_APPLICABLE" if threshold == 0 else "UNKNOWN"
            evaluation.update(electronic_iteration_count=len(iterations), effective_ediff=threshold,
                              effective_nelm=values.get("NELM"), numeric_checks=checks, numeric_convergence=numeric,
                              final_electronic_row=row or None,
                              budget_reached=(len(iterations) >= values["NELM"]) if isinstance(values.get("NELM"), int) else None)
            evaluation["nonfinite_findings"] = [{"field": key, "reference": it.get("oszicar", {}).get("reference")} for it in iterations for key, value in it.get("oszicar", {}).items() if isinstance(value, dict) and value.get("finding") == "nonfinite_or_overflow"]
            if evaluation["nonfinite_findings"]:
                result["conflicts"].append({"code": "nonfinite_electronic_values", "evaluation_index": evaluation["index"]})
            if numeric == "FAIL" and evaluation["native_convergence"] is True:
                result["conflicts"].append({"code": "native_numeric_convergence_conflict", "evaluation_index": evaluation["index"]})
            if row.get("algorithm") not in (None, "DAV", "RMM"):
                result["mode"]["supported"] = False
                result["mode"]["reasons"].append("unsupported_table_algorithm")
        poscar, contcar = _geometry(parsed.get("POSCAR")), _geometry(parsed.get("CONTCAR"))
        species = [s for s, n in zip(current["species"], current["counts"]) for _ in range(n)]
        if len(current["species"]) != len(current["counts"]) or sum(current["counts"]) != current["nions"] or not species:
            result["conflicts"].append({"code": "native_species_count_mismatch"})
        if current["endpoint"] and len(current["endpoint"]["cartesian"]) != len(species):
            result["conflicts"].append({"code": "endpoint_atom_count_mismatch"})
        initial = None
        if current["cell"] and current["initial_positions"]:
            initial = {"cell": (current["initial_positions"].get("cell") or current["cell"])["matrix"], "cartesian": current["initial_positions"]["cartesian"], "species": species}
        endpoint = current["endpoint"]
        geometry = None
        if endpoint and endpoint["cell"]:
            geometry = {"cell": endpoint["cell"]["matrix"], "cartesian": endpoint["cartesian"], "species": species}
            geometry["fractional"] = (np.asarray(geometry["cartesian"]) @ np.linalg.inv(np.asarray(geometry["cell"]))).tolist()
            geometry["geometry_id"] = stable_hash(geometry)
        comparisons = {"initial_poscar": compare_geometry(initial, poscar), "endpoint_poscar": compare_geometry(geometry, poscar),
                       "endpoint_contcar": compare_geometry(geometry, contcar), "fixed_cell": {"status": "unavailable"}}
        if geometry and initial:
            delta = max(float(np.max(np.abs(np.asarray(c["matrix"]) - np.asarray(initial["cell"])))) for c in current["cell_history"])
            comparisons["fixed_cell"] = {"status": "match" if delta <= CELL_TOLERANCE else "mismatch", "max_cell_difference_angstrom": delta}
        flags = poscar["constraints"] if poscar else None
        constraint_match = poscar is not None and contcar is not None and flags == contcar["constraints"]
        result["constraints"] = {"flags": flags, "source_role": "POSCAR", "input_binding": "unestablished",
                                 "contcar_flags_match": constraint_match, "partial": bool(flags and any(any(f) and not all(f) for f in flags))}
        if geometry:
            geometry["constraints"] = flags
        result["endpoint"] = {"geometry": geometry, "evaluation_index": endpoint["evaluation_index"] if endpoint else None,
                              "comparisons": comparisons, "is_last_complete_evaluation": bool(endpoint and current["evaluations"] and endpoint["evaluation_index"] == len(current["evaluations"]) - 1),
                              "is_final_segment": selected == len(segments) - 1,
                              "force_summary": _force_summary(endpoint["forces"], flags) if endpoint and (flags is None or len(flags) == len(endpoint["forces"])) else None,
                              "force_vectors": endpoint["forces"] if endpoint else None,
                              "force_half_last_place": endpoint["force_half_last_place"] if endpoint else None}
        if endpoint and poscar is None and result["endpoint"]["force_summary"]:
            result["endpoint"]["force_summary"]["free"] = None
            result["endpoint"]["force_summary"]["fixed"] = None
        result["ionic"] = {"native_stopping_reference": current["ionic_stop"], "ediffg": values.get("EDIFFG"),
                           "criterion_kind": "force" if isinstance(values.get("EDIFFG"), (int, float)) and values["EDIFFG"] < 0 else "energy" if isinstance(values.get("EDIFFG"), (int, float)) and values["EDIFFG"] > 0 else "step_budget" if values.get("EDIFFG") == 0 else "unknown",
                           "budget_reached": (current["evaluations"][-1]["native_ionic_label"] >= values["NSW"]) if current["evaluations"] and type(values.get("NSW")) is int and values["NSW"] > 0 else None,
                           "budget_basis": "native ionic label; not accepted optimizer step count",
                           "accepted_step_count": None}
        result["input_comparison"] = []
        incar = parsed.get("INCAR", {}).get("settings", {})
        for key, value in incar.items():
            if key in values and values[key] != value:
                result["input_comparison"].append({"tag": key, "filesystem": value, "effective": values[key], "status": "conflict"})
        if not result["mode"]["supported"]:
            result.update(status="unsupported", exit_code=2)
    except (OSError, ValueError, SourceSnapshotError) as error:
        if getattr(error, "record", None):
            result["sources"].append(error.record)
        result.update(status="invalid", exit_code=1, error=str(error))
    return _finish_result(result)


def _finish_result(result):
    nonfinite = []
    def finite_tree(value, path):
        if isinstance(value, float) and not math.isfinite(value):
            nonfinite.append(path)
            return None
        if isinstance(value, dict):
            return {k: finite_tree(v, path + "." + k) for k, v in value.items()}
        if isinstance(value, list):
            return [finite_tree(v, path + f"[{i}]") for i, v in enumerate(value)]
        return value
    result = finite_tree(result, "evidence")
    if nonfinite:
        result["conflicts"].append({"code": "nonfinite_derived_values", "locations": nonfinite})
    sources = result["sources"]
    result["snapshot_id"] = stable_hash({"schema_version": 1, "sources": [{"role": s["role"], "sha256": s["sha256"], "bytes_read": s["bytes_read"]} for s in sources]})
    result["coverage"] = {"all_sources_complete": all(s["coverage"] == "complete" for s in sources),
                          "all_sources_stable": all(s["stable"] for s in sources),
                          "full_electronic_history": bool(result.get("selected_segment") and result["selected_segment"]["evaluations"] and not result["selected_segment"]["conflicts"] and not result["selected_segment"]["trailing_incomplete"] and all(e["complete"] for e in result["selected_segment"]["evaluations"]))}
    return result
