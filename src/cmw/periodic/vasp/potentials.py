"""Ordered, explicit POTPAW selection and atomic POTCAR assembly."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Sequence

from cmw.structure.xyz import ELEMENTS


@dataclass(frozen=True)
class Dataset:
    element: str
    label: str | None


def read_poscar_species(path: str | Path) -> tuple[str, ...]:
    """Read explicit VASP 5/6 species blocks without inferring atom identities."""
    with Path(path).expanduser().open(encoding="utf-8") as stream:
        lines = [stream.readline() for _ in range(7)]
    species = tuple(lines[5].split())
    if species and all(token.isdecimal() for token in species):
        raise ValueError("VASP 4 POSCAR has no explicit species; supply a VASP 5/6 POSCAR")
    if not species or any(symbol not in ELEMENTS for symbol in species):
        raise ValueError("POSCAR requires explicit, unambiguous element symbols on line 6")
    counts = lines[6].split()
    if len(counts) != len(species) or any(not value.isdecimal() for value in counts):
        raise ValueError("POSCAR species/count lines are missing or inconsistent")
    if not any(int(value) > 0 for value in counts):
        raise ValueError("POSCAR must declare at least one atom")
    return species


def resolve_root(root: str | Path | None = None) -> Path:
    selected = root if root is not None else os.environ.get("CMW_VASP_POTCAR_ROOT")
    if not selected:
        raise ValueError("Supply --potcar-root PATH or set CMW_VASP_POTCAR_ROOT to a local POTPAW library")
    path = Path(selected).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"POTPAW root is not a directory: {path}")
    return path


def parse_overrides(values: Sequence[str], species: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        element, separator, label = value.partition("=")
        if not separator or not element or not label:
            raise ValueError("Each --pot must be ELEMENT=VARIANT")
        if element not in species:
            raise ValueError(f"Override element {element!r} is absent from POSCAR")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", label):
            raise ValueError("Potential name must be a single directory entry beneath the POTPAW root")
        if element in overrides and overrides[element] != label:
            raise ValueError(f"Conflicting duplicate overrides for {element}")
        overrides[element] = label
    return overrides


def parse_potcar(data: bytes) -> tuple[Dataset, ...]:
    """Read terminated datasets using TITEL and/or VRHFIN, never exposing raw text.

    Metadata and dataset boundaries establish identity, not numerical validity
    or scientific suitability of the licensed potential.
    """
    blocks = re.split(r"(?m)^\s*End of Dataset\s*$", data.decode("latin-1"))
    if len(blocks) < 2 or blocks[-1].strip():
        raise ValueError("POTCAR is empty, truncated, or lacks an End of Dataset boundary")
    datasets = []
    for index, block in enumerate(blocks[:-1], 1):
        titles = re.findall(r"(?m)^\s*TITEL\s*=\s*([^\r\n]+)", block)
        vrhfins = re.findall(r"(?m)^\s*VRHFIN\s*=\s*([A-Z][a-z]?)\s*:", block)
        if len(titles) > 1 or len(vrhfins) > 1:
            raise ValueError(f"POTCAR dataset {index} has duplicate identity metadata")
        label = None
        title_element = None
        if titles:
            tokens = titles[0].split()
            match = re.fullmatch(r"([A-Z][a-z]?)(?:[_\.0-9][A-Za-z0-9_.-]*)?", tokens[1]) if len(tokens) >= 2 else None
            if match is None or match[1] not in ELEMENTS:
                raise ValueError(f"POTCAR dataset {index} has unrecognized TITEL metadata")
            label, title_element = tokens[1], match[1]
        element = vrhfins[0] if vrhfins else title_element
        if element not in ELEMENTS:
            raise ValueError(f"POTCAR dataset {index} lacks recognizable element metadata")
        if title_element is not None and title_element != element:
            raise ValueError(f"POTCAR dataset {index} has conflicting TITEL/VRHFIN elements")
        datasets.append(Dataset(element, label))
    return tuple(datasets)


def validate_species(species: Sequence[str], datasets: Sequence[Dataset]) -> None:
    if len(species) != len(datasets):
        raise ValueError(f"POTCAR dataset count mismatch: expected {len(species)}, found {len(datasets)}")
    actual = tuple(dataset.element for dataset in datasets)
    if tuple(species) != actual:
        raise ValueError(f"POTCAR species order mismatch: expected {' '.join(species)}, found {' '.join(actual)}")


def _potential_path(root: Path, label: str) -> Path:
    directory = (root / label).resolve()
    if not directory.is_relative_to(root) or directory == root:
        raise ValueError("Potential directory escapes the POTPAW root")
    if not directory.is_dir():
        raise ValueError(f"Missing potential directory: {root / label}")
    path = (directory / "POTCAR").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Potential file escapes the POTPAW root")
    if not path.is_file():
        raise ValueError(f"Missing POTCAR file: {directory / 'POTCAR'}")
    return path


def list_potentials(element: str, root: str | Path | None = None) -> tuple[str, ...]:
    if element not in ELEMENTS:
        raise ValueError(f"Invalid element symbol: {element!r}")
    library = resolve_root(root)
    labels = []
    for entry in sorted(library.iterdir()):
        if entry.name != element and not entry.name.startswith((element + "_", element + ".")):
            continue
        if not entry.is_dir():
            continue
        datasets = parse_potcar(_potential_path(library, entry.name).read_bytes())
        validate_species((element,), datasets)
        labels.append(entry.name)
    return tuple(labels)


def check_potcar(poscar: str | Path = "POSCAR", potcar: str | Path = "POTCAR") -> tuple[Dataset, ...]:
    species = read_poscar_species(poscar)
    datasets = parse_potcar(Path(potcar).expanduser().read_bytes())
    validate_species(species, datasets)
    return datasets


def build_potcar(
    poscar: str | Path = "POSCAR",
    output: str | Path = "POTCAR",
    *,
    root: str | Path | None = None,
    pots: Sequence[str] = (),
    force: bool = False,
) -> dict[str, object]:
    species = read_poscar_species(poscar)
    overrides = parse_overrides(pots, species)
    library = resolve_root(root)
    destination = Path(output).expanduser().absolute()
    if destination.resolve().is_relative_to(library) or destination.resolve() == Path(poscar).expanduser().resolve():
        raise ValueError("Output must not replace the POSCAR or any file in the POTPAW library")
    if os.path.lexists(destination) and not force:
        raise FileExistsError(f"Output already exists: {destination}; use --force to replace it")
    labels = tuple(overrides.get(element, element) for element in species)
    chunks = []
    for element, label in zip(species, labels):
        data = _potential_path(library, label).read_bytes()
        validate_species((element,), parse_potcar(data))
        # Keep source bytes intact; separate a final unterminated boundary line.
        chunks.append(data if data.endswith(b"\n") else data + b"\n")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".cmw-potcar-", delete=False) as stream:
            temporary = Path(stream.name)
            for data in chunks:
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        datasets = parse_potcar(temporary.read_bytes())
        validate_species(species, datasets)
        if force:
            os.replace(temporary, destination)
        else:
            # An exclusive hard-link publication also protects against a racing writer.
            os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "species": species,
        "potentials": labels,
        "overrides": overrides,
        "root": str(library),
        "output": str(destination),
        "datasets": len(datasets),
    }
