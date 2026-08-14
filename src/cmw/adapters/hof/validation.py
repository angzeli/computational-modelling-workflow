"""HOF-only fragment and hydrogen-bond validation rules."""

from __future__ import annotations

from collections import Counter

from cmw.core.provenance import stable_hash

from .models import HofSystem


HOF_DONOR_ELEMENTS = frozenset({"N", "O", "S"})
HOF_ACCEPTOR_ELEMENTS = frozenset({"F", "N", "O", "S", "Cl", "Br", "I"})


class HofValidationError(ValueError):
    """Raised when a HOF domain definition is scientifically inconsistent."""


def validate_hof_system(system: HofSystem) -> None:
    """Validate a complete two-fragment HOF dimer and its H-bond metadata."""

    if len(system.fragments) != 2:
        raise HofValidationError(
            "HOF interaction systems require exactly two fragments"
        )
    fragment_ids = [fragment.fragment_id for fragment in system.fragments]
    if len(set(fragment_ids)) != len(fragment_ids):
        raise HofValidationError("fragment ids must be unique")

    atom_count = system.geometry.atom_count
    if any(atom.ghost for atom in system.geometry.atoms):
        raise HofValidationError("HOF system geometry cannot contain ghost atoms")
    expected_atoms = set(range(atom_count))
    supplied: set[int] = set()
    for fragment in system.fragments:
        invalid = [
            index for index in fragment.atom_indices if index not in expected_atoms
        ]
        if invalid:
            raise HofValidationError(
                f"fragment {fragment.fragment_id!r} has invalid atom index {invalid[0]}"
            )
        overlap = supplied.intersection(fragment.atom_indices)
        if overlap:
            raise HofValidationError(
                f"fragment definitions overlap at atom index {min(overlap)}"
            )
        supplied.update(fragment.atom_indices)
    if supplied != expected_atoms:
        missing = sorted(expected_atoms - supplied)
        raise HofValidationError(
            f"fragment definitions do not cover every atom; missing {missing}"
        )
    if sum(fragment.charge for fragment in system.fragments) != system.charge:
        raise HofValidationError("fragment charges do not sum to the system charge")

    if len(system.hydrogen_bonds) != system.expected_hydrogen_bonds:
        raise HofValidationError(
            "hydrogen-bond count does not match hydrogen_bonds.expected_count"
        )
    bond_ids = [bond.bond_id for bond in system.hydrogen_bonds]
    if len(set(bond_ids)) != len(bond_ids):
        raise HofValidationError("hydrogen-bond ids must be unique")

    atom_to_fragment = system.atom_to_fragment
    signatures: list[tuple[str, str, str, frozenset[str]]] = []
    symmetry_records: list[dict[str, object]] = []
    directions: Counter[tuple[str, str]] = Counter()
    for bond in system.hydrogen_bonds:
        referenced = (bond.donor_atom, bond.hydrogen_atom, bond.acceptor_atom)
        invalid = [index for index in referenced if index not in expected_atoms]
        if invalid:
            raise HofValidationError(
                f"hydrogen bond {bond.bond_id!r} references invalid atom {invalid[0]}"
            )
        donor = system.geometry.atoms[bond.donor_atom]
        hydrogen = system.geometry.atoms[bond.hydrogen_atom]
        acceptor = system.geometry.atoms[bond.acceptor_atom]
        if donor.ghost or donor.element not in HOF_DONOR_ELEMENTS:
            raise HofValidationError(
                f"hydrogen bond {bond.bond_id!r} has incompatible donor element {donor.element}"
            )
        if hydrogen.ghost or hydrogen.element != "H":
            raise HofValidationError(
                f"hydrogen bond {bond.bond_id!r} hydrogen_atom is not hydrogen"
            )
        if acceptor.ghost or acceptor.element not in HOF_ACCEPTOR_ELEMENTS:
            raise HofValidationError(
                f"hydrogen bond {bond.bond_id!r} has incompatible acceptor element {acceptor.element}"
            )

        donor_fragment = atom_to_fragment[bond.donor_atom]
        hydrogen_fragment = atom_to_fragment[bond.hydrogen_atom]
        acceptor_fragment = atom_to_fragment[bond.acceptor_atom]
        if donor_fragment != hydrogen_fragment:
            raise HofValidationError(
                f"hydrogen bond {bond.bond_id!r} donor and hydrogen belong to different fragments"
            )
        if acceptor_fragment == donor_fragment:
            raise HofValidationError(
                f"hydrogen bond {bond.bond_id!r} acceptor must belong to the opposite fragment"
            )
        directions[(donor_fragment, acceptor_fragment)] += 1
        signatures.append(
            (
                donor.element,
                hydrogen.element,
                acceptor.element,
                frozenset({donor_fragment, acceptor_fragment}),
            )
        )
        symmetry_records.append(dict(bond.symmetry_metadata))

    if system.symmetry_equivalent and signatures:
        if len(set(signatures)) != 1:
            raise HofValidationError(
                "symmetry-equivalent hydrogen bonds have inconsistent atom roles"
            )
        if len({stable_hash(record) for record in symmetry_records}) != 1:
            raise HofValidationError(
                "symmetry-equivalent hydrogen bonds have inconsistent symmetry metadata"
            )
        if len(directions) > 1 and len(set(directions.values())) != 1:
            raise HofValidationError(
                "symmetry-equivalent hydrogen bonds have inconsistent fragment directions"
            )


__all__ = [
    "HOF_ACCEPTOR_ELEMENTS",
    "HOF_DONOR_ELEMENTS",
    "HofValidationError",
    "validate_hof_system",
]
