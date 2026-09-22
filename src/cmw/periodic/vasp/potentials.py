"""Ordered, explicit POTPAW selection and atomic POTCAR assembly."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date as calendar_date
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping, Sequence

from cmw.structure.xyz import ELEMENTS


@dataclass(frozen=True)
class Dataset:
    element: str
    label: str | None
    family: str | None = None
    date: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class PotcarSelection:
    """One selected byte snapshot, ready for publication without rereading sources."""

    content: bytes
    record: dict[str, object]
    source_paths: tuple[Path, ...]


def read_poscar_blocks(source: str | Path | bytes) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Read explicit block identities/counts; this is not geometry validation."""
    if isinstance(source, bytes):
        lines = source.decode("utf-8").splitlines()[:7]
    else:
        with Path(source).expanduser().open(encoding="utf-8") as stream:
            lines = [stream.readline() for _ in range(7)]
    if len(lines) < 7:
        raise ValueError("POSCAR species/count lines are missing or inconsistent")
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
    return species, tuple(int(value) for value in counts)


def read_poscar_species(path: str | Path) -> tuple[str, ...]:
    """Read explicit VASP 5/6 species blocks without inferring atom identities."""
    return read_poscar_blocks(path)[0]


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
    boundaries = list(re.finditer(rb"(?m)^[ \t]*End of Dataset[ \t]*(?:\r?\n|$)", data))
    if not boundaries or data[boundaries[-1].end():].strip():
        raise ValueError("POTCAR is empty, truncated, or lacks an End of Dataset boundary")
    datasets = []
    start = 0
    for index, boundary in enumerate(boundaries, 1):
        chunk = data[start:boundary.end()]
        block = data[start:boundary.start()].decode("latin-1")
        start = boundary.end()
        titles = re.findall(r"(?m)^[ \t]*TITEL[ \t]*=[ \t]*([^\r\n]*)", block)
        vrhfin_fields = re.findall(r"(?m)^[ \t]*VRHFIN[ \t]*=[ \t]*([^\r\n]*)", block)
        vrhfins = []
        for value in vrhfin_fields:
            match = re.match(r"([A-Z][a-z]?)\s*:", value)
            if match is None:
                raise ValueError(f"POTCAR dataset {index} has unrecognized VRHFIN metadata")
            vrhfins.append(match[1])
        if len(titles) > 1 or len(vrhfins) > 1:
            raise ValueError(f"POTCAR dataset {index} has duplicate identity metadata")
        label = None
        family = None
        date = None
        title_element = None
        if titles:
            tokens = titles[0].split()
            match = re.fullmatch(r"([A-Z][a-z]?)(?:[_\.0-9][A-Za-z0-9_.-]*)?", tokens[1]) if len(tokens) >= 2 else None
            if match is None or match[1] not in ELEMENTS:
                raise ValueError(f"POTCAR dataset {index} has unrecognized TITEL metadata")
            label, title_element = tokens[1], match[1]
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.+-]*", tokens[0]):
                family = tokens[0]
            if len(tokens) >= 3 and re.fullmatch(r"\d{2}(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\d{4}", tokens[2]):
                date = tokens[2]
                months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
                try:
                    calendar_date(int(date[5:]), months.index(date[2:5]) + 1, int(date[:2]))
                except ValueError as exc:
                    raise ValueError(f"POTCAR dataset {index} has an invalid dataset date") from exc
        element = vrhfins[0] if vrhfins else title_element
        if element not in ELEMENTS:
            raise ValueError(f"POTCAR dataset {index} lacks recognizable element metadata")
        if title_element is not None and title_element != element:
            raise ValueError(f"POTCAR dataset {index} has conflicting TITEL/VRHFIN elements")
        datasets.append(Dataset(element, label, family, date, hashlib.sha256(chunk).hexdigest()))
    return tuple(datasets)


def _requirements(value: Mapping[str, object] | None, species: Sequence[str]) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Potential requirements must be an object")
    allowed = {"family", "dataset_date", "library_release", "variants", "sha256"}
    if set(value) - allowed:
        raise ValueError("Unknown potential requirement: " + ", ".join(sorted(set(value) - allowed)))
    result = dict(value)
    for key, expected in result.items():
        if key in {"variants", "sha256"}:
            if not isinstance(expected, Mapping) or any(element not in species for element in expected):
                raise ValueError(f"{key} requirements must map declared elements to identities")
            if any(not isinstance(item, str) or not item.strip() for item in expected.values()):
                raise ValueError(f"{key} requirements must contain non-empty strings")
            if key == "sha256" and any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in expected.values()):
                raise ValueError("sha256 requirements must be lowercase hexadecimal SHA-256 values")
            result[key] = dict(expected)
        elif not isinstance(expected, str) or not expected.strip():
            raise ValueError(f"{key} requirement must be a non-empty string")
    return result


def inspect_potcar(
    data: bytes,
    *,
    species: Sequence[str] | None = None,
    counts: Sequence[int] | None = None,
    requirements: Mapping[str, object] | None = None,
    library_release: str | None = None,
    strict: bool = False,
) -> dict[str, object]:
    """Inspect an assembled byte snapshot without inferring selection or release.

    Identity requirements yield structured comparisons. Malformed metadata and
    species mismatches raise ValueError; an unavailable or mismatched requested
    identity is represented by ``identity_valid=False``.
    """
    datasets = parse_potcar(data)
    selected_species = tuple(species) if species is not None else tuple(d.element for d in datasets)
    validate_species(selected_species, datasets)
    if counts is not None and (len(counts) != len(datasets) or any(type(count) is not int or count < 0 for count in counts)):
        raise ValueError("POTCAR block counts must match the species sequence")
    if library_release is not None and (not isinstance(library_release, str) or not library_release.strip()):
        raise ValueError("library_release must be a non-empty separately declared identifier")
    expected = _requirements(requirements, selected_species)
    comparisons: list[dict[str, object]] = []
    missing = []
    records = []
    def compare(field: str, wanted: object, observed: object, index: int | None = None) -> None:
        comparisons.append({"field": field, "expected": wanted, "observed": observed,
                            "block_index": index, "status": "unestablished" if observed is None else
                            ("verified" if wanted == observed else "mismatch")})
    for index, dataset in enumerate(datasets):
        record = asdict(dataset)
        record["dataset_date"] = record.pop("date")
        record["block_index"] = index
        records.append(record)
        for field in ("label", "family", "dataset_date"):
            if record[field] is None:
                missing.append(f"datasets[{index}].{field}")
        if strict:
            for field in ("label", "family"):
                if record[field] is None:
                    compare(field, "established identity", None, index)
        for field in ("family", "dataset_date"):
            if field in expected:
                compare(field, expected[field], record[field], index)
        for requirement, field in (("variants", "label"), ("sha256", "sha256")):
            if dataset.element in expected.get(requirement, {}):
                compare(field, expected[requirement][dataset.element], record[field], index)
    if library_release is None:
        missing.append("library_release")
    if "library_release" in expected:
        compare("library_release", expected["library_release"], library_release)
    return {
        "schema_version": 1, "species": list(selected_species),
        "counts": list(counts) if counts is not None else None,
        "dataset_count": len(datasets), "datasets": records,
        "potcar_sha256": hashlib.sha256(data).hexdigest(),
        "library_release": {"value": library_release, "basis": "caller_declared" if library_release else "unestablished"},
        "requirements": expected, "comparability": comparisons,
        "unestablished": missing, "strict_identity": strict,
        "identity_valid": all(item["status"] == "verified" for item in comparisons),
        "selection_status": "unassessed", "scientific_suitability": "unassessed",
    }


def _require_identity(record: Mapping[str, object]) -> None:
    if not record["identity_valid"]:
        failures = [f"{item['field']} {item['status']} (block {item['block_index']})"
                    for item in record["comparability"] if item["status"] != "verified"]
        raise ValueError("POTCAR identity requirements failed: " + "; ".join(failures))


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
        if datasets[0].label is not None and datasets[0].label != entry.name:
            raise ValueError(f"Potential variant mismatch: requested {entry.name}, metadata identifies {datasets[0].label}")
        labels.append(entry.name)
    return tuple(labels)


def check_potcar(poscar: str | Path = "POSCAR", potcar: str | Path = "POTCAR") -> tuple[Dataset, ...]:
    species = read_poscar_species(poscar)
    datasets = parse_potcar(Path(potcar).expanduser().read_bytes())
    validate_species(species, datasets)
    return datasets


def resolve_potcar(
    poscar: str | Path | bytes = "POSCAR",
    *,
    root: str | Path | None = None,
    pots: Sequence[str] = (),
    requirements: Mapping[str, object] | None = None,
    library_release: str | None = None,
    strict: bool = False,
) -> PotcarSelection:
    """Resolve selected files once and inspect the exact bytes to be published."""
    species, counts = read_poscar_blocks(poscar)
    overrides = parse_overrides(pots, species)
    library = resolve_root(root)
    labels = tuple(overrides.get(element, element) for element in species)
    selected: dict[Path, tuple[bytes, Dataset]] = {}
    chunks = []
    blocks = []
    for index, (element, count, label) in enumerate(zip(species, counts, labels)):
        source = _potential_path(library, label)
        if source not in selected:
            data = source.read_bytes()
            datasets = parse_potcar(data)
            validate_species((element,), datasets)
            selected[source] = (data, datasets[0])
        data, dataset = selected[source]
        validate_species((element,), (dataset,))
        if dataset.label is not None and dataset.label != label:
            raise ValueError(f"Potential variant mismatch: requested {label}, metadata identifies {dataset.label}")
        if element in overrides and dataset.label is None:
            raise ValueError(f"Potential variant unestablished for explicit selection {label}")
        chunk = data if data.endswith(b"\n") else data + b"\n"
        chunks.append(chunk)
        blocks.append({
            "block_index": index, "element": element, "count": count,
            "requested_variant": label, "selection_origin": "override" if element in overrides else "suffix_free",
            "resolved_variant": dataset.label, "source_path": str(source),
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "assembled_chunk_sha256": hashlib.sha256(chunk).hexdigest(),
        })
    content = b"".join(chunks)
    record = inspect_potcar(content, species=species, counts=counts, requirements=requirements,
                            library_release=library_release, strict=strict)
    _require_identity(record)
    record.update({"root": str(library), "potentials": list(labels), "overrides": overrides,
                   "blocks": blocks, "selected_sources": [str(path) for path in selected],
                   "selection_status": "verified" if all(block["resolved_variant"] is not None for block in blocks)
                   else "element_only"})
    return PotcarSelection(content, record, tuple(selected))


def build_potcar(
    poscar: str | Path = "POSCAR",
    output: str | Path = "POTCAR",
    *,
    root: str | Path | None = None,
    pots: Sequence[str] = (),
    force: bool = False,
    requirements: Mapping[str, object] | None = None,
    library_release: str | None = None,
    strict: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    library = resolve_root(root)
    destination = Path(output).expanduser().absolute()
    if destination.resolve().is_relative_to(library) or destination.resolve() == Path(poscar).expanduser().resolve():
        raise ValueError("Output must not replace the POSCAR or any file in the POTPAW library")
    if os.path.lexists(destination) and not force:
        raise FileExistsError(f"Output already exists: {destination}; use --force to replace it")
    if not destination.parent.is_dir():
        raise ValueError(f"Output parent directory does not exist: {destination.parent}")
    selection = resolve_potcar(poscar, root=library, pots=pots, requirements=requirements,
                               library_release=library_release, strict=strict)
    record = selection.record
    result = {
        "species": tuple(record["species"]), "potentials": tuple(record["potentials"]),
        "overrides": record["overrides"], "root": record["root"], "output": str(destination),
        "datasets": record["dataset_count"], "selection": record, "written": False,
    }
    if dry_run:
        return result
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".cmw-potcar-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(selection.content)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != selection.content:
            raise ValueError("Staged POTCAR bytes differ from the validated selection")
        if force:
            os.replace(temporary, destination)
        else:
            # An exclusive hard-link publication also protects against a racing writer.
            os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {**result, "written": True}
