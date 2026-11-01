"""Explicit, streaming LOCPOT plateau analysis; no generation binding is implied.

VASP stores x fastest, then y, then z. Potential values are already eV, unlike
CHGCAR density values: neither cell volume nor grid size rescales the potential.
Only planar means and the source observation are retained. The public analysis
layer must separately bind this observation to the electronic endpoint.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from cmw.periodic.vasp.inputs import parse_poscar
from cmw.periodic.vasp.result_sources import SnapshotReader, SourceSnapshotError
from cmw.periodic.vasp.results import number


DEFAULT_THRESHOLDS = {
    "min_width_angstrom": 2.0,
    "max_variation_ev": 0.05,
    "max_abs_slope_ev_per_angstrom": 0.01,
    "atomic_exclusion_angstrom": 2.0,
    "discontinuity_exclusion_angstrom": 1.0,
    "jump_threshold_ev": 0.5,
}


def _value(settings: Mapping, name: str):
    item = settings.get(name)
    return item.get("value") if isinstance(item, Mapping) else item


def _finding(code: str, message: str, **evidence) -> dict:
    return {"code": code, "message": message, "evidence": evidence}


def _line(lines) -> str:
    line = next(lines)
    if not line.text.endswith("\n"):
        raise ValueError("Incomplete trailing LOCPOT record")
    return line.text


def _header(lines) -> dict:
    header = [_line(lines) for _ in range(8)]
    counts = header[6].split()
    if not counts or any(not token.isdecimal() for token in counts):
        raise ValueError("Explicit species and nonnegative integer counts are required")
    atom_count = sum(int(token) for token in counts)
    if atom_count <= 0:
        raise ValueError("LOCPOT must contain at least one atom")
    if header[7].strip().lower().startswith("s"):
        header.append(_line(lines))
    for _ in range(atom_count):
        header.append(_line(lines))
    parsed = parse_poscar("".join(header))
    if parsed["status"] != "valid":
        raise ValueError("Unsupported or malformed LOCPOT structure: " +
                         "; ".join(item["message"] for item in parsed["findings"]))
    geometry = {"cell": parsed["cell"], "species": parsed["atom_species"],
                "cartesian": parsed["cartesian_coordinates"]}
    offset = 9 if header[7].strip().lower().startswith("s") else 8
    if parsed["scale"] == [1.0] and parsed["coordinate_mode"] == "Direct":
        geometry["cell_tokens"] = [[number(t) for t in row.split()] for row in header[2:5]]
        geometry["fractional_tokens"] = [[number(t) for t in row.split()[:3]]
                                         for row in header[offset:offset + atom_count]]
    # Retain output rounding bounds to distinguish an apparent mismatch from
    # a proven conflict. Scale factors are coordinate conventions, not errors.
    scales = parsed["scale"]
    if all(scale > 0 for scale in scales):
        factors = np.asarray(scales * 3 if len(scales) == 1 else scales)
        cell_error = np.asarray([[number(t)["half_last_place"] for t in row.split()]
                                 for row in header[2:5]]) * factors
        coordinate_error = np.asarray([[number(t)["half_last_place"] for t in row.split()[:3]]
                                       for row in header[offset:offset + atom_count]])
        if parsed["coordinate_mode"] == "Direct":
            coordinates = np.asarray(parsed["coordinates"])
            cart_error = (coordinate_error @ abs(np.asarray(parsed["cell"]))
                          + abs(coordinates) @ cell_error + coordinate_error @ cell_error)
        else:
            cart_error = coordinate_error * factors
        geometry["printed_bounds"] = {
            "position_angstrom": float(np.max(np.linalg.norm(cart_error, axis=1))),
            "cell_angstrom": float(np.max(cell_error)),
        }
    return geometry


def _grid(lines, dimensions: tuple[int, int, int], axis: int, source_bytes: int) -> tuple[list[float], list[float], str]:
    total = math.prod(dimensions)
    if total > source_bytes:
        raise ValueError("Grid cannot fit within the observed source size")
    sums = [0.0] * dimensions[axis]
    precision_sums = [0.0] * dimensions[axis]
    digest = hashlib.sha256()
    index = 0
    stride = math.prod(dimensions[:axis])
    while index < total:
        tokens = _line(lines).split()
        if not tokens:
            continue
        if index + len(tokens) > total:
            raise ValueError("Too many potential values in a grid block")
        for token in tokens:
            parsed = number(token)
            value = parsed["value"]
            if value is None:
                raise ValueError("Nonfinite or unsupported potential value")
            plane = (index // stride) % dimensions[axis]
            sums[plane] += value
            precision_sums[plane] += parsed["half_last_place"]
            # Canonical decoded values allow different exponent spellings but
            # do not let equal planar averages hide unequal spin potentials.
            digest.update((value.hex() + "\n").encode("ascii"))
            index += 1
    divisor = total // dimensions[axis]
    profile = [value / divisor for value in sums]
    precision = [value / divisor for value in precision_sums]
    if not all(math.isfinite(value) for value in (*profile, *precision)):
        raise ValueError("Potential planar accumulation overflowed")
    return profile, precision, digest.hexdigest()


def _slab_interval(positions: np.ndarray, length: float) -> tuple[float, float] | None:
    ordered = np.sort(np.unique(np.mod(positions, length)))
    gaps = np.diff(np.append(ordered, ordered[0] + length))
    widest = float(np.max(gaps))
    candidates = np.flatnonzero(np.abs(gaps - widest) <= 1e-8)
    if len(candidates) != 1:
        return None
    index = int(candidates[0])
    # The empty arc follows the upper surface and precedes the lower surface
    # of the next periodic slab. Labels use the positive selected lattice axis.
    return float(ordered[index]), float(ordered[index] + widest)


def _plateaus(profile, precision, positions, length, atom_positions, intervals, thresholds) -> dict:
    empty_arc = _slab_interval(atom_positions, length)
    jumps = [((positions[i] + length / len(profile) / 2) % length)
             for i, value in enumerate(profile)
             if abs(profile[(i + 1) % len(profile)] - value) > thresholds["jump_threshold_ev"]]
    results = {}
    for side, bounds in intervals.items():
        start, end = map(float, bounds)
        requested_span = end - start
        start %= length
        width = (end - start) % length
        entry = {"value": None, "units": "eV", "status": "UNKNOWN",
                 "bounds_angstrom": [start, (start + width) % length],
                 "unwrapped_bounds_angstrom": [start, start + width],
                 "width_angstrom": width, "variation_ev": None,
                 "slope_ev_per_angstrom": None, "half_last_place_ev": None,
                 "points": 0, "findings": []}
        results[side] = entry
        reasons = entry["findings"]
        if width == 0 or abs(requested_span) >= length - 1e-10:
            reasons.append(_finding("vacuum.interval_ambiguous", "Interval must cover less than one periodic cell"))
            continue
        relative = np.mod(np.asarray(positions) - start, length)
        selected = np.flatnonzero(relative <= width + 1e-10)
        entry["points"] = int(len(selected))
        if len(selected) < 3:
            reasons.append(_finding("vacuum.undersampled", "At least three planar grid points are required"))
            continue
        x = relative[selected]
        y = np.asarray(profile)[selected]
        variation = float(np.ptp(y))
        centered = x - float(np.mean(x))
        slope = float(np.dot(centered, y - float(np.mean(y))) / np.dot(centered, centered))
        entry.update(value=float(np.mean(y)), variation_ev=variation, slope_ev_per_angstrom=slope,
                     half_last_place_ev=float(np.mean(np.asarray(precision)[selected])))
        if empty_arc is None:
            reasons.append(_finding("vacuum.side_ambiguous", "Atomic projection has no unique slab unwrap"))
        else:
            upper, lower = empty_arc
            unwrapped_start = upper + (start - upper) % length
            unwrapped_end = unwrapped_start + width
            midpoint = (upper + lower) / 2
            valid = (unwrapped_start >= upper - 1e-8 and unwrapped_end <= lower + 1e-8
                     and (unwrapped_end < midpoint - 1e-8 if side == "upper"
                          else unwrapped_start > midpoint + 1e-8))
            if not valid:
                reasons.append(_finding("vacuum.side_ambiguous", "Interval crosses or disagrees with the declared slab side",
                                        empty_arc_angstrom=[upper, lower], side=side))
        if width < thresholds["min_width_angstrom"]:
            reasons.append(_finding("vacuum.too_narrow", "Vacuum interval is narrower than the minimum"))
        # Check continuous intervals, not only sampled points: a coarse grid
        # must not conceal an atom or a dipole discontinuity between planes.
        def separation(point):
            distance = (point - start) % length
            return 0.0 if distance <= width else min(distance - width, length - distance)
        if any(separation(float(atom)) < thresholds["atomic_exclusion_angstrom"] for atom in atom_positions):
            reasons.append(_finding("vacuum.atomic_exclusion", "Interval is too close to a projected atom"))
        if any(separation(jump) < thresholds["discontinuity_exclusion_angstrom"] for jump in jumps):
            reasons.append(_finding("vacuum.discontinuity", "Interval is too close to a potential discontinuity"))
        if variation > thresholds["max_variation_ev"]:
            reasons.append(_finding("vacuum.not_flat", "Potential variation exceeds its threshold"))
        if abs(slope) > thresholds["max_abs_slope_ev_per_angstrom"]:
            reasons.append(_finding("vacuum.sloped", "Absolute fitted potential slope exceeds its threshold"))
        entry["status"] = "PASS" if not reasons else (
            "UNKNOWN" if any(item["code"] == "vacuum.side_ambiguous" for item in reasons) else "FAIL")
    return results


def read_locpot(path: str | Path, *, axis: int | None = None, settings: Mapping,
                intervals: Mapping[str, Sequence[float]], thresholds: Mapping | None = None) -> dict:
    """Read explicit side intervals in Angstrom; axis is zero-based (a/b/c).

    ``end < start`` selects a periodically wrapped interval. Intervals must fit
    entirely on the declared lower/upper side; equally spaced atom projections
    or an interval crossing the vacuum midplane leave that assignment unknown.
    Pointwise identical collinear electrostatic channels are accepted as one
    scalar potential. This is conservative and does not average spin channels.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        if set(thresholds) - set(limits):
            raise ValueError("Unknown vacuum threshold")
        limits.update(thresholds)
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in limits.values()):
        raise ValueError("Vacuum thresholds must be finite nonnegative numbers")
    if limits["min_width_angstrom"] <= 0 or limits["jump_threshold_ev"] <= 0:
        raise ValueError("Minimum width and jump threshold must be positive")
    if set(intervals) - {"upper", "lower"}:
        raise ValueError("Intervals must be labelled upper and/or lower")
    for bounds in intervals.values():
        if len(bounds) != 2 or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                   or not math.isfinite(value) for value in bounds):
            raise ValueError("Each interval needs two finite Angstrom bounds")
        if not math.isfinite(bounds[1] - bounds[0]):
            raise ValueError("Interval width must be finite")
    if axis is None:
        idipol = _value(settings, "IDIPOL")
        axis = idipol - 1 if idipol in (1, 2, 3) and not isinstance(idipol, bool) else None
    if axis is not None and (isinstance(axis, bool) or not isinstance(axis, int) or axis not in (0, 1, 2)):
        raise ValueError("Axis must be 0, 1 or 2")
    reader = SnapshotReader(path, "LOCPOT")
    result = {"status": "UNKNOWN", "evidence_status": "UNKNOWN", "source": reader.record, "findings": [],
              "geometry": None, "axis": axis, "length_angstrom": None,
              "grid": None, "potential_semantics": "unestablished", "thresholds": limits,
              "sides": {}, "profile": None, "generation_binding": "unestablished"}
    if axis is None:
        result["findings"].append(_finding("vacuum.axis_missing", "Explicit axis or IDIPOL=1/2/3 evidence is required"))
        return result
    if not intervals:
        result["findings"].append(_finding("vacuum.intervals_missing", "Explicit side-labelled intervals are required"))
        return result
    unsupported = []
    if _value(settings, "LSORBIT") is not False or _value(settings, "LNONCOLLINEAR") is not False:
        unsupported.append("Explicit non-SOC, noncollinear-false evidence is required")
    spin = _value(settings, "ISPIN")
    if spin not in (1, 2):
        unsupported.append("Only ISPIN=1/2 LOCPOT is supported")
    if _value(settings, "LVHAR") is True and _value(settings, "LVTOT") is not True:
        result["potential_semantics"] = "hartree_plus_ionic"
    else:
        result["potential_semantics"] = "xc_containing" if _value(settings, "LVTOT") is True else "unestablished"
        unsupported.append("Effective Hartree-plus-ionic (LVHAR) potential evidence is required")
    try:
        with reader:
            lines = iter(reader)
            geometry = _header(lines)
            result["geometry"] = geometry
            cell = np.asarray(geometry["cell"], dtype=float)
            vector = cell[axis]
            length = float(np.linalg.norm(vector))
            result["length_angstrom"] = length
            for other in range(3):
                if other != axis and abs(float(np.dot(vector, cell[other]))) > 1e-8 * length * float(np.linalg.norm(cell[other])):
                    unsupported.append("Selected lattice axis must be perpendicular to both in-plane vectors")
            profiles, precisions, identities = [], [], []
            dimensions = None
            while True:
                try:
                    text = _line(lines)
                except StopIteration:
                    break
                tokens = text.split()
                if not tokens:
                    continue
                if len(tokens) != 3 or any(not token.isdecimal() or int(token) < 1 for token in tokens):
                    raise ValueError("Expected three positive grid dimensions; unsupported trailing section")
                current = tuple(map(int, tokens))
                if dimensions is not None and current != dimensions:
                    raise ValueError("Spin grid dimensions differ")
                dimensions = current
                if len(profiles) >= 2:
                    raise ValueError("More than two potential blocks are unsupported")
                profile, precision, identity = _grid(lines, dimensions, axis, reader.record["stat_before"]["size"])
                profiles.append(profile)
                precisions.append(precision)
                identities.append(identity)
            if not profiles:
                raise ValueError("LOCPOT contains no potential grid")
            result["grid"] = list(dimensions)
            result["spin_blocks"] = len(profiles)
            if spin in (1, 2) and len(profiles) != spin:
                unsupported.append("Potential block count disagrees with ISPIN")
            if len(profiles) == 2 and identities[0] != identities[1]:
                unsupported.append("Collinear potential channels are not pointwise identical")
            result["channel_compatibility"] = "identical" if len(profiles) == 2 and identities[0] == identities[1] else "single" if len(profiles) == 1 else "different"
            positions = [index * length / dimensions[axis] for index in range(dimensions[axis])]
            result["profile"] = {"positions_angstrom": positions, "values_ev": profiles[0],
                                 "half_last_place_ev": precisions[0],
                                 "units": "eV", "grid_order": "x-fastest, then y, then z"}
            atom_positions = np.dot(np.asarray(geometry["cartesian"]), vector / length)
            result["sides"] = _plateaus(profiles[0], precisions[0], positions, length, atom_positions, intervals, limits)
    except SourceSnapshotError as exc:
        result["status"] = "UNKNOWN" if exc.code == "source.missing" else "FAIL"
        result["evidence_status"] = result["status"]
        result["findings"].append(_finding(exc.code, str(exc)))
        return result
    except (ValueError, StopIteration, OverflowError) as exc:
        result["status"] = "FAIL"
        result["evidence_status"] = "FAIL"
        result["findings"].append(_finding("vacuum.malformed", str(exc) or "Incomplete LOCPOT"))
        return result
    if not reader.record["stable"] or reader.record["coverage"] != "complete":
        result["findings"].append(_finding("vacuum.unstable_source", "LOCPOT changed or was incompletely observed"))
        return result
    if unsupported:
        result["status"] = "UNSUPPORTED"
        result["evidence_status"] = "UNSUPPORTED"
        result["findings"].extend(_finding("vacuum.unsupported", message) for message in unsupported)
    else:
        result["evidence_status"] = "PASS"
        states = {item["status"] for item in result["sides"].values()}
        result["status"] = "FAIL" if "FAIL" in states else "UNKNOWN" if "UNKNOWN" in states else "PASS"
    return result
