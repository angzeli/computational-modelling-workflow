"""Validated, non-mutating DOS and reciprocal-path array transformations.

Readers supply physical arrays and explicit references. No energy-zero inference,
band-edge classification, file I/O, plotting, or materials-specific policy lives
here. NumPy is the only dependency of this leaf module.
"""

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray


def finite_array(values: ArrayLike, *, ndim: int, name: str) -> NDArray:
    """Return a detached finite float array of the requested dimensionality."""
    array = np.array(values, dtype=float, copy=True)
    if array.ndim != ndim or not array.size or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a nonempty finite {ndim}-D array")
    return array


def shift_energy(energies: ArrayLike, reference_ev: float) -> NDArray:
    """Subtract an explicit scalar eV reference without modifying the input."""
    values = np.array(energies, dtype=float, copy=True)
    if not values.size or not np.isfinite(values).all() or not np.isfinite(reference_ev):
        raise ValueError("Energy values and reference must be finite and nonempty")
    return values - reference_ev


def prepare_dos(
    energies_ev: ArrayLike, curves: Mapping[str, ArrayLike], *,
    reference_ev: float, divisor: float = 1.0,
) -> tuple[NDArray, dict[str, NDArray]]:
    """Shift a DOS grid and divide every physical curve by one extensive divisor.

    Curves have shape (nenergy,); spin channels, if any, are caller-labelled
    positive physical channels. No spin doubling, mirroring or smoothing occurs.
    Tiny negative round-off down to -1e-12 is retained, never clipped.
    """
    energy = finite_array(energies_ev, ndim=1, name="DOS energy")
    if len(energy) < 2 or np.any(np.diff(energy) <= 0):
        raise ValueError("DOS energy must be strictly increasing")
    if not curves or not np.isfinite(divisor) or divisor <= 0:
        raise ValueError("DOS curves and a finite positive divisor are required")
    result = {}
    for label, values in curves.items():
        density = finite_array(values, ndim=1, name=f"DOS {label}")
        if density.shape != energy.shape or np.any(density < -1e-12):
            raise ValueError(f"DOS {label} has a mismatched shape or negative density")
        result[label] = density / divisor
    return shift_energy(energy, reference_ev), result


def aggregate_projections(
    site_elements: Sequence[str], site_channels: Sequence[Mapping[str, ArrayLike]],
    groups: Mapping[str, Sequence[str]],
) -> dict[str, NDArray]:
    """Sum labelled components across sites, independent of species ordering.

    Each site must provide every component requested by each group, with one
    common energy-array shape. Keys returned are ``element-group``. This is
    internal projection aggregation, not a claim of closure to total DOS.
    """
    if not site_elements or len(site_elements) != len(site_channels) or not groups:
        raise ValueError("Site identities, projections and groups must be nonempty and aligned")
    result, shape = {}, None
    for element, channels in zip(site_elements, site_channels):
        for group, components in groups.items():
            if not components or len(set(components)) != len(components):
                raise ValueError(f"Empty or repeated components in {group}")
            arrays = []
            for component in components:
                if component not in channels:
                    raise ValueError(f"Missing projection {element}/{component}")
                array = finite_array(channels[component], ndim=1, name=component)
                if shape is None:
                    shape = array.shape
                if array.shape != shape or np.any(array < -1e-12):
                    raise ValueError("Projection shape mismatch or negative density")
                arrays.append(array)
            key = f"{element}-{group}"
            summed = np.sum(arrays, axis=0)
            result[key] = result.get(key, np.zeros_like(summed)) + summed
    return result


@dataclass(frozen=True)
class ReciprocalPath:
    """Ordered distances (inverse angstrom), segment slices and branch breaks."""

    distances: NDArray
    intended_fractional: NDArray
    segments: tuple[slice, ...]
    disconnected_before: tuple[int, ...]


def prepare_path(
    lattice_rows_angstrom: ArrayLike, endpoints: ArrayLike, sampled_fractional: ArrayLike,
    points_per_segment: int, *, tolerance_angstrom_inverse: float = 2e-5,
) -> ReciprocalPath:
    """Recover ordered interpolation modulo reciprocal translations, without folding.

    ``endpoints`` has shape (nsegment, 2, 3). Every segment includes both
    endpoints, including intentional repetitions. Disconnected endpoints share
    a plotting distance but are never given an artificial connecting length.
    The reciprocal row basis is ``2*pi*inverse(A).T``.
    """
    lattice = finite_array(lattice_rows_angstrom, ndim=2, name="lattice")
    ends = finite_array(endpoints, ndim=3, name="endpoints")
    actual = finite_array(sampled_fractional, ndim=2, name="sampled k points")
    if lattice.shape != (3, 3) or abs(np.linalg.det(lattice)) < 1e-12:
        raise ValueError("A nonsingular 3x3 row lattice is required")
    if ends.shape[1:] != (2, 3) or not isinstance(points_per_segment, int) or points_per_segment < 2:
        raise ValueError("Expected endpoint pairs and at least two samples per segment")
    if not np.isfinite(tolerance_angstrom_inverse) or tolerance_angstrom_inverse <= 0:
        raise ValueError("Path tolerance must be finite and positive")
    intended = np.concatenate([np.linspace(a, b, points_per_segment) for a, b in ends])
    if actual.shape != intended.shape:
        raise ValueError("Output k-point count does not match ordered segment interpolation")
    reciprocal = 2 * np.pi * np.linalg.inv(lattice).T
    delta = actual - intended
    if np.max(np.linalg.norm((delta - np.rint(delta)) @ reciprocal, axis=1)) > tolerance_angstrom_inverse:
        raise ValueError("Output k points differ from requested ordered interpolation")
    distances, slices, breaks, offset = [], [], [], 0.0
    for index, (start, end) in enumerate(ends):
        length = float(np.linalg.norm((end - start) @ reciprocal))
        if length <= 1e-12:
            raise ValueError("Zero-length path segment")
        if index and not np.allclose(start, ends[index - 1, 1], atol=1e-10, rtol=0):
            breaks.append(index)
        distances.extend(offset + np.linspace(0, length, points_per_segment))
        slices.append(slice(index * points_per_segment, (index + 1) * points_per_segment))
        offset += length
    return ReciprocalPath(np.asarray(distances), intended, tuple(slices), tuple(breaks))
