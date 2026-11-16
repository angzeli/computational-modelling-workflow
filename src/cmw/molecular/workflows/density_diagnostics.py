"""Advisory electron-density cube diagnostics (issue #52).

One streaming pass over a density cube; values are never all held in memory.
The result is stored beside, not inside, the analysis result: its policy is
outside target identity, so changing thresholds re-evaluates without rerunning
Multiwfn, and existing results can be diagnosed too.

The integral mostly reflects grid resolution near nuclear cusps, not box
coverage; boundary density and face margins are the coverage signals. These
diagnostics establish sanity, not grid convergence.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Mapping

from cmw.core.provenance import file_hash

from .cube import BOHR_TO_ANGSTROM, scan_cube

SCHEMA_VERSION = 1
DEFAULT_ISOVALUE = 0.001
_ELECTRONS = re.compile(r"Total/Alpha/Beta electrons:\s+([-+0-9.EeDd]+)")
_FACES = ("x-", "x+", "y-", "y+", "z-", "z+")
CAVEAT = ("The integral mostly reflects grid resolution near nuclear cusps, not box coverage; "
          "boundary density and face margins are the coverage signals. Sanity only, not grid convergence.")


def wavefunction_electrons(log_path: Path | None) -> float | None:
    """Electron count Multiwfn reported for the loaded wavefunction, if present."""
    if log_path is None or not log_path.is_file():
        return None
    matches = _ELECTRONS.findall(log_path.read_text(encoding="utf-8", errors="replace"))
    if not matches:
        return None
    value = float(matches[-1].replace("D", "E").replace("d", "e"))
    return value if math.isfinite(value) else None


def cube_metrics(path: Path, *, isovalue: float = DEFAULT_ISOVALUE) -> dict[str, object]:
    """Stream one density cube; malformed, truncated or nonfinite data raises."""
    if not math.isfinite(isovalue) or isovalue <= 0:
        raise ValueError("isovalue must be finite and positive")
    state: dict[str, object] = {}

    def start(metadata) -> None:
        state["dims"] = metadata.dimensions
        state["min"] = math.inf
        state["max"] = -math.inf
        state["face_max"] = dict.fromkeys(_FACES, -math.inf)
        state["iso_faces"] = set()
        state["weighted"] = 0.0

    def visit(index: int, value: float) -> None:
        nx, ny, nz = state["dims"]
        iz = index % nz
        iy = (index // nz) % ny
        ix = index // (ny * nz)
        if value < state["min"]:
            state["min"] = value
        if value > state["max"]:
            state["max"] = value
        face_max = state["face_max"]
        weight = 1.0
        for name, position, count in (("x", ix, nx), ("y", iy, ny), ("z", iz, nz)):
            if position < 2 and value > face_max[name + "-"]:
                face_max[name + "-"] = value
            if position >= count - 2 and value > face_max[name + "+"]:
                face_max[name + "+"] = value
            if position == 0 or position == count - 1:
                weight *= 0.5
                if value >= isovalue:
                    state["iso_faces"].add(name + ("-" if position == 0 else "+"))
        state["weighted"] += weight * value

    metadata = scan_cube(path, visit, on_header=start)
    vectors = [tuple(component / BOHR_TO_ANGSTROM for component in vector)
               for vector in metadata.vectors_angstrom]
    (a, b, c), (d, e, f), (g, h, i) = vectors
    cell = abs(a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g))
    nx, ny, nz = metadata.dimensions
    face_max = state["face_max"]
    boundary_face = max(_FACES, key=lambda face: face_max[face])
    return {
        "dimensions": list(metadata.dimensions),
        "cell_volume_bohr3": cell,
        "box_volume_bohr3": cell * (nx - 1) * (ny - 1) * (nz - 1),
        "min_density": state["min"],
        "max_density": state["max"],
        "outer_two_layer_max_by_face": face_max,
        "max_boundary_density": face_max[boundary_face],
        "max_boundary_face": boundary_face,
        "isovalue": isovalue,
        "isosurface_reaches_boundary": bool(state["iso_faces"]),
        "isosurface_boundary_faces": sorted(state["iso_faces"]),
        "trapezoidal_integral": state["weighted"] * cell,
    }


def evaluate(
    metrics: Mapping[str, object],
    electrons: float | None,
    *,
    max_boundary_density: float | None = None,
    max_electron_discrepancy: float | None = None,
) -> dict[str, object]:
    """Advisory unless a threshold is given; requested gates fail closed."""
    discrepancy = None if electrons is None else float(metrics["trapezoidal_integral"]) - electrons
    gates, reasons = {}, []
    if max_boundary_density is not None:
        passed = float(metrics["max_boundary_density"]) <= max_boundary_density
        gates["max_boundary_density"] = {"threshold": max_boundary_density, "passed": passed}
        if not passed:
            reasons.append(f"boundary density {metrics['max_boundary_density']:.3e} at "
                           f"{metrics['max_boundary_face']} exceeds {max_boundary_density:.3e}")
    if max_electron_discrepancy is not None:
        if discrepancy is None:
            gates["max_electron_discrepancy"] = {"threshold": max_electron_discrepancy, "passed": False}
            reasons.append("electron-count evidence is missing from the Multiwfn log")
        else:
            passed = abs(discrepancy) <= max_electron_discrepancy
            gates["max_electron_discrepancy"] = {"threshold": max_electron_discrepancy, "passed": passed}
            if not passed:
                reasons.append(f"electron discrepancy {discrepancy:+.4f} exceeds ±{max_electron_discrepancy}")
    status = "ADVISORY" if not gates else ("FAIL" if reasons else "PASS")
    return {"status": status, "wavefunction_electrons": electrons,
            "electron_discrepancy": discrepancy, "gates": gates, "reasons": reasons}


def diagnose(
    density_cube: Path,
    *,
    expected_sha256: str,
    log_path: Path | None,
    isovalue: float = DEFAULT_ISOVALUE,
    max_boundary_density: float | None = None,
    max_electron_discrepancy: float | None = None,
) -> dict[str, object]:
    for name, value in (("max_boundary_density", max_boundary_density),
                        ("max_electron_discrepancy", max_electron_discrepancy)):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"{name} must be finite and nonnegative")
    actual = file_hash(density_cube)
    if actual != expected_sha256:
        raise ValueError("density cube differs from the recorded result artifact")
    metrics = cube_metrics(density_cube, isovalue=isovalue)
    electrons = wavefunction_electrons(log_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "density-cube-diagnostics",
        "density_cube": {"path": str(density_cube), "sha256": actual},
        "multiwfn_log": str(log_path) if log_path is not None else None,
        "policy": {"isovalue": isovalue, "max_boundary_density": max_boundary_density,
                   "max_electron_discrepancy": max_electron_discrepancy},
        "metrics": metrics,
        **evaluate(metrics, electrons, max_boundary_density=max_boundary_density,
                   max_electron_discrepancy=max_electron_discrepancy),
        "caveat": CAVEAT,
    }
