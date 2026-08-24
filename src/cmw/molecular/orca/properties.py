"""Fail-closed extraction of quantitative ground-state ORCA properties."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Mapping, Sequence


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
ORBITAL_ROW = re.compile(
    rf"^\s*(\d+)\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*$"
)
DIPOLE_VECTOR = re.compile(
    rf"^\s*Total Dipole Moment\s*:\s*({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*$",
    re.I,
)
DIPOLE_MAGNITUDE = re.compile(
    rf"^\s*Magnitude\s*\(Debye\)\s*:\s*({NUMBER})\s*$", re.I
)
HIRSHFELD_ROW = re.compile(
    rf"^\s*(\d+)\s+([A-Z][a-z]?)\s+({NUMBER})\s+({NUMBER})\s*$"
)
FMO_HEADER = re.compile(r"ANALYZING ORBITALS:\s*HOMO=\s*(\d+)\s+LUMO=\s*(\d+)", re.I)
FMO_ROW = re.compile(
    rf"^\s*(\d+)\s*-\s*([A-Z][a-z]?)\s+"
    rf"({NUMBER})\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*$"
)
FRAGMENT_MO_ROW = re.compile(
    rf"^\s*(\d+)\s+({NUMBER})\s+({NUMBER})\s*:\s*(.*?)\s*$"
)


def _number(value: str) -> float:
    parsed = float(value.replace("D", "E").replace("d", "e"))
    if not math.isfinite(parsed):
        raise ValueError("ORCA property output contains a non-finite number")
    return parsed


@dataclass(frozen=True)
class OrbitalEnergy:
    index: int
    occupation: float
    energy_hartree: float
    energy_ev: float


@dataclass(frozen=True)
class AtomicCharge:
    atom_index: int
    element: str
    charge: float
    spin: float


@dataclass(frozen=True)
class FrontierAtomicPopulation:
    atom_index: int
    element: str
    homo_mulliken: float
    homo_loewdin: float
    lumo_mulliken: float
    lumo_loewdin: float


@dataclass(frozen=True)
class FragmentOrbitalPopulation:
    orbital_index: int
    energy_hartree: float
    occupation: float
    contributions: tuple[float, ...]


@dataclass(frozen=True)
class OrcaGroundStateProperties:
    final_energy_hartree: float
    orbitals: tuple[OrbitalEnergy, ...]
    homo_index: int
    lumo_index: int
    homo_energy_ev: float
    lumo_energy_ev: float
    gap_ev: float
    dipole_vector_au: tuple[float, float, float]
    dipole_debye: float
    hirshfeld_charges: tuple[AtomicCharge, ...]
    frontier_atomic_populations: tuple[FrontierAtomicPopulation, ...] = ()
    loewdin_fragment_orbital_populations: tuple[
        FragmentOrbitalPopulation, ...
    ] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "orbitals": [asdict(item) for item in self.orbitals],
            "hirshfeld_charges": [
                asdict(item) for item in self.hirshfeld_charges
            ],
            "frontier_atomic_populations": [
                asdict(item) for item in self.frontier_atomic_populations
            ],
            "loewdin_fragment_orbital_populations": [
                asdict(item) for item in self.loewdin_fragment_orbital_populations
            ],
        }

    def fragment_charges(
        self, fragments: Mapping[str, Sequence[int]]
    ) -> dict[str, float]:
        _validate_partition(fragments, len(self.hirshfeld_charges))
        by_index = {item.atom_index: item.charge for item in self.hirshfeld_charges}
        return {
            fragment_id: sum(by_index[index] for index in indices)
            for fragment_id, indices in fragments.items()
        }

    def frontier_fragment_populations(
        self, fragments: Mapping[str, Sequence[int]], *, scheme: str = "loewdin"
    ) -> dict[str, dict[str, float]]:
        if not self.frontier_atomic_populations:
            raise ValueError("ORCA output lacks frontier atomic population analysis")
        _validate_partition(fragments, len(self.frontier_atomic_populations))
        suffix = scheme.casefold()
        if suffix not in {"mulliken", "loewdin"}:
            raise ValueError("frontier population scheme must be Mulliken or Loewdin")
        rows = {item.atom_index: item for item in self.frontier_atomic_populations}
        values: dict[str, dict[str, float]] = {"HOMO": {}, "LUMO": {}}
        for fragment_id, indices in fragments.items():
            values["HOMO"][fragment_id] = sum(
                float(getattr(rows[index], f"homo_{suffix}")) for index in indices
            )
            values["LUMO"][fragment_id] = sum(
                float(getattr(rows[index], f"lumo_{suffix}")) for index in indices
            )
        for orbital in values:
            total = sum(values[orbital].values())
            if not math.isfinite(total) or abs(total) <= 1.0e-12:
                raise ValueError(f"{orbital} fragment populations cannot be normalized")
            values[orbital] = {
                key: value / total for key, value in values[orbital].items()
            }
        return values

    def orbital_fragment_populations(
        self,
        fragment_ids: Sequence[str],
        orbital_indices: Sequence[int],
    ) -> dict[str, dict[str, float]]:
        """Return normalized Loewdin fragment contributions for selected MOs."""

        identifiers = tuple(str(item) for item in fragment_ids)
        if not identifiers or len(set(identifiers)) != len(identifiers):
            raise ValueError("fragment identities must be unique and non-empty")
        rows = {
            item.orbital_index: item
            for item in self.loewdin_fragment_orbital_populations
        }
        selected: dict[str, dict[str, float]] = {}
        for orbital_index in orbital_indices:
            if orbital_index not in rows:
                raise ValueError(
                    f"ORCA output lacks Loewdin fragment populations for MO {orbital_index}"
                )
            row = rows[orbital_index]
            if len(row.contributions) != len(identifiers):
                raise ValueError(
                    "ORCA fragment-population columns do not match fragment identities"
                )
            total = sum(row.contributions)
            if not math.isfinite(total) or abs(total) <= 1.0e-12:
                raise ValueError(
                    f"MO {orbital_index} fragment populations cannot be normalized"
                )
            selected[f"MO_{orbital_index}"] = {
                fragment_id: contribution / total
                for fragment_id, contribution in zip(
                    identifiers, row.contributions, strict=True
                )
            }
        return selected


def _validate_partition(
    fragments: Mapping[str, Sequence[int]], atom_count: int
) -> None:
    if not fragments or any(not str(key).strip() for key in fragments):
        raise ValueError("fragment identities must be non-empty")
    supplied = [int(index) for indices in fragments.values() for index in indices]
    if len(supplied) != len(set(supplied)):
        raise ValueError("fragment atom partitions overlap")
    if set(supplied) != set(range(atom_count)):
        raise ValueError("fragment atom partitions must cover every atom exactly once")


def _last_section(lines: Sequence[str], heading: str) -> list[str]:
    positions = [index for index, line in enumerate(lines) if line.strip() == heading]
    if not positions:
        return []
    return list(lines[positions[-1] + 1 :])


def _orbital_energies(lines: Sequence[str]) -> tuple[OrbitalEnergy, ...]:
    section = _last_section(lines, "ORBITAL ENERGIES")
    rows: list[OrbitalEnergy] = []
    started = False
    for line in section:
        match = ORBITAL_ROW.match(line)
        if match:
            started = True
            index, occupation, hartree, ev = match.groups()
            rows.append(
                OrbitalEnergy(
                    int(index),
                    _number(occupation),
                    _number(hartree),
                    _number(ev),
                )
            )
            continue
        if started and (not line.strip() or set(line.strip()) == {"-"}):
            break
    if not rows or len({item.index for item in rows}) != len(rows):
        raise ValueError("ORCA output lacks one unambiguous orbital-energy table")
    return tuple(rows)


def _dipole(lines: Sequence[str]) -> tuple[tuple[float, float, float], float]:
    vectors = [match.groups() for line in lines if (match := DIPOLE_VECTOR.match(line))]
    magnitudes = [match.group(1) for line in lines if (match := DIPOLE_MAGNITUDE.match(line))]
    if not vectors or not magnitudes:
        raise ValueError("ORCA output lacks a complete dipole moment")
    return tuple(_number(item) for item in vectors[-1]), _number(magnitudes[-1])  # type: ignore[return-value]


def _hirshfeld(lines: Sequence[str]) -> tuple[AtomicCharge, ...]:
    section = _last_section(lines, "HIRSHFELD ANALYSIS")
    rows: list[AtomicCharge] = []
    started = False
    for line in section:
        match = HIRSHFELD_ROW.match(line)
        if match:
            started = True
            index, element, charge, spin = match.groups()
            rows.append(
                AtomicCharge(int(index), element, _number(charge), _number(spin))
            )
            continue
        if started and line.strip().startswith("TOTAL"):
            break
    if not rows or tuple(item.atom_index for item in rows) != tuple(range(len(rows))):
        raise ValueError("ORCA output lacks consecutive Hirshfeld atomic charges")
    return tuple(rows)


def _frontier_populations(
    lines: Sequence[str], atom_count: int
) -> tuple[tuple[FrontierAtomicPopulation, ...], tuple[int, int] | None]:
    headers = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := FMO_HEADER.search(line))
    ]
    if not headers:
        return (), None
    start, header = headers[-1]
    rows: list[FrontierAtomicPopulation] = []
    for line in lines[start + 1 :]:
        match = FMO_ROW.match(line)
        if match:
            index, element, hm, hl, lm, ll = match.groups()
            rows.append(
                FrontierAtomicPopulation(
                    int(index),
                    element,
                    _number(hm),
                    _number(hl),
                    _number(lm),
                    _number(ll),
                )
            )
        elif rows and (not line.strip() or set(line.strip()) == {"-"}):
            break
    if len(rows) != atom_count or tuple(item.atom_index for item in rows) != tuple(
        range(atom_count)
    ):
        raise ValueError("ORCA frontier population table does not cover every atom")
    return tuple(rows), (int(header.group(1)), int(header.group(2)))


def _loewdin_fragment_orbital_populations(
    lines: Sequence[str],
) -> tuple[FragmentOrbitalPopulation, ...]:
    section = _last_section(lines, "LOEWDIN FRAGMENT POPULATIONS PER MO")
    if not section:
        return ()
    rows: list[FragmentOrbitalPopulation] = []
    column_count: int | None = None
    for line in section:
        match = FRAGMENT_MO_ROW.match(line)
        if not match:
            continue
        orbital, energy, occupation, raw_contributions = match.groups()
        values = tuple(
            _number(value) for value in re.findall(NUMBER, raw_contributions)
        )
        if not values:
            raise ValueError("ORCA Loewdin fragment MO row lacks contributions")
        if column_count is None:
            column_count = len(values)
        elif len(values) != column_count:
            raise ValueError(
                "ORCA Loewdin fragment MO table has inconsistent columns"
            )
        rows.append(
            FragmentOrbitalPopulation(
                int(orbital), _number(energy), _number(occupation), values
            )
        )
    if not rows or len({item.orbital_index for item in rows}) != len(rows):
        raise ValueError("ORCA Loewdin fragment MO table is missing or ambiguous")
    return tuple(rows)


def parse_ground_state_properties(text: str) -> OrcaGroundStateProperties:
    """Parse one closed-shell ground-state result without guessing absent values."""

    from .status import parse_orca_output

    evidence = parse_orca_output(text)
    if evidence.final_energy_hartree is None:
        raise ValueError("ORCA output lacks a final electronic energy")
    orbitals = _orbital_energies(text.splitlines())
    occupied = [item for item in orbitals if item.occupation > 1.0e-8]
    virtual = [item for item in orbitals if item.occupation <= 1.0e-8]
    if not occupied or not virtual:
        raise ValueError("ORCA orbital table does not identify HOMO and LUMO")
    homo = occupied[-1]
    lumo = next((item for item in virtual if item.index > homo.index), None)
    if lumo is None:
        raise ValueError("ORCA orbital table lacks a LUMO after the HOMO")
    vector, magnitude = _dipole(text.splitlines())
    charges = _hirshfeld(text.splitlines())
    frontier, indices = _frontier_populations(text.splitlines(), len(charges))
    fragment_orbitals = _loewdin_fragment_orbital_populations(text.splitlines())
    if indices is not None and indices != (homo.index, lumo.index):
        raise ValueError("frontier population indices contradict orbital energies")
    return OrcaGroundStateProperties(
        evidence.final_energy_hartree,
        orbitals,
        homo.index,
        lumo.index,
        homo.energy_ev,
        lumo.energy_ev,
        lumo.energy_ev - homo.energy_ev,
        vector,
        magnitude,
        charges,
        frontier,
        fragment_orbitals,
    )


__all__ = [
    "AtomicCharge",
    "FrontierAtomicPopulation",
    "FragmentOrbitalPopulation",
    "OrcaGroundStateProperties",
    "OrbitalEnergy",
    "parse_ground_state_properties",
]
