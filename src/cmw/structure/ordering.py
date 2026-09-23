"""Stable species grouping shared by molecular and periodic structure writers."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from .xyz import ELEMENTS


def normalize_species_order(
    symbols: tuple[str, ...], requested: Iterable[str] | None = None
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


def group_species(
    symbols: tuple[str, ...], requested: Iterable[str] | None = None
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Return species order and a stable, bijective output-to-input permutation."""

    order = normalize_species_order(symbols, requested)
    output_to_input = tuple(
        index for element in order for index, symbol in enumerate(symbols)
        if symbol == element
    )
    if sorted(output_to_input) != list(range(len(symbols))):
        raise ValueError("species grouping did not produce a bijective atom mapping")
    return order, output_to_input
