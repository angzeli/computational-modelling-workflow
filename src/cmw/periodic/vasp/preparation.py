"""Explicit four-file VASP preparation, without execution or hidden profiles."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
from typing import Mapping

from cmw.core.preparation_publication import (
    content_identity, inspect_publication, publication_plan,
    publish_preparation,
)
from cmw.core.provenance import stable_hash
from .inputs import INPUT_NAMES, KNOWN_TAGS, check_input_bytes, parse_incar, parse_kpoints, parse_poscar
from .potentials import resolve_potcar


class PreparationError(ValueError):
    def __init__(self, message: str, *, code: str = "PREPARATION_REJECTED", findings: list | None = None):
        super().__init__(message)
        self.code = code
        self.findings = findings or []


def _cmw_version() -> str:
    try:
        return version("computational-modelling-workflow")
    except PackageNotFoundError:
        return "uninstalled-source"


@dataclass(frozen=True)
class PreparationSpec:
    """Caller-owned data; relative sources are anchored to the defining file."""

    path: Path
    data: dict
    profile: dict
    profile_base: Path
    sources: tuple[dict, ...]


def _object(value: object, allowed: set[str], context: str, required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise PreparationError(f"{context} must be an object")
    if set(value) - allowed:
        raise PreparationError(f"Unknown {context} fields: {', '.join(sorted(set(value) - allowed))}")
    if required - set(value):
        raise PreparationError(f"Missing {context} fields: {', '.join(sorted(required - set(value)))}")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError(f"Duplicate JSON definition: {key}")
        result[key] = value
    return result


def _decode(data: bytes) -> dict:
    def nonfinite(value: str) -> None:
        raise PreparationError(f"Nonfinite JSON value: {value}")
    value = json.loads(data, object_pairs_hook=_unique, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise PreparationError("JSON root must be an object")
    return value


def _source(value: str | Path, base: Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise PreparationError("Source location must be a nonempty path")
    path = Path(value).expanduser()
    path = path if path.is_absolute() else base / path
    # Resolve dot components, but refuse symlink traversal like publication.
    import os
    path = Path(os.path.abspath(path))
    if path != path.resolve(strict=True):
        raise PreparationError(f"Symlink source is unsupported: {path}")
    return path


def _source_record(role: str, recorded: object, path: Path, data: bytes) -> dict:
    return {"role": role, "recorded_location": str(recorded), "resolved_location": str(path),
            **content_identity(data)}


def load_spec(path: str | Path) -> PreparationSpec:
    source = _source(path, Path.cwd())
    raw = source.read_bytes()
    data = _object(_decode(raw), {"schema_version", "calculation", "structure", "profile", "overrides",
                                "runtime", "baseline", "source_record"}, "specification",
                   {"schema_version", "calculation", "structure", "profile"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise PreparationError("Only preparation schema_version 1 is supported")
    if data["calculation"] not in ("static", "fixed-cell-relaxation"):
        raise PreparationError("Supported calculation types: static, fixed-cell-relaxation")
    sources = [_source_record("specification", path, source, raw)]
    profile = data["profile"]
    base = source.parent
    if isinstance(profile, str):
        location = _source(profile, base)
        raw = location.read_bytes()
        sources.append(_source_record("profile", profile, location, raw))
        profile = _decode(raw)
        base = location.parent
    profile = _object(profile, {"name", "incar", "kpoints", "potentials"}, "profile",
                      {"name", "incar", "kpoints", "potentials"})
    if not isinstance(profile["name"], str) or not profile["name"].strip():
        raise PreparationError("Profile name must be a nonempty caller-owned identifier")
    return PreparationSpec(source, data, profile, base, tuple(sources))


def _incar_text(settings: Mapping[str, object]) -> bytes:
    def scalar(value: object) -> str:
        if type(value) is bool:
            return ".TRUE." if value else ".FALSE."
        if type(value) in (int, float):
            if not math.isfinite(value):
                raise PreparationError("INCAR numbers must be finite")
            return str(value)
        if isinstance(value, str) and value.strip() and not any(char in value for char in "\n\r;#!=\"'"):
            return value
        raise PreparationError("INCAR values must be typed scalars or numeric arrays, without assignment syntax")
    lines = []
    for key, value in sorted(settings.items()):
        if isinstance(value, list):
            if not value or any(type(item) not in (int, float) for item in value):
                raise PreparationError(f"{key} requires a nonempty numeric array")
            rendered = " ".join(scalar(item) for item in value)
        else:
            rendered = scalar(value)
        lines.append(f"{key} = {rendered}\n")
    return "".join(lines).encode("utf-8")


def _incar_settings(value: object, context: str) -> dict:
    if not isinstance(value, dict):
        raise PreparationError(f"{context} must be an object")
    settings = {}
    for key, item in value.items():
        if not isinstance(key, str) or key.upper() not in KNOWN_TAGS:
            raise PreparationError(f"Unsupported preparation INCAR tag: {key}")
        tag = key.upper()
        if tag in settings:
            raise PreparationError(f"Ambiguous case-insensitive INCAR definition: {tag}")
        settings[tag] = item
    parsed = parse_incar(_incar_text(settings).decode("utf-8"))
    if parsed["status"] != "valid":
        raise PreparationError(f"Invalid {context}", findings=parsed["findings"])
    return parsed["settings"]


def _kpoints(value: object) -> tuple[bytes, dict]:
    mesh = _object(value, {"mode", "mesh", "shift"}, "kpoints", {"mode", "mesh", "shift"})
    if mesh["mode"] not in ("Gamma", "Monkhorst-Pack"):
        raise PreparationError("Only Gamma and Monkhorst-Pack regular meshes are supported")
    if not isinstance(mesh["mesh"], list) or len(mesh["mesh"]) != 3 or any(type(n) is not int or n <= 0 for n in mesh["mesh"]):
        raise PreparationError("Mesh must contain three positive integers")
    if not isinstance(mesh["shift"], list) or len(mesh["shift"]) != 3 or any(type(n) not in (int, float) or not math.isfinite(n) for n in mesh["shift"]):
        raise PreparationError("Shift must contain three finite numbers")
    text = ("CMW explicit regular mesh\n0\n" + mesh["mode"] + "\n" +
            " ".join(str(n) for n in mesh["mesh"]) + "\n" +
            " ".join(str(n) for n in mesh["shift"]) + "\n")
    return text.encode(), {key: parse_kpoints(text)[key] for key in ("mode", "mesh", "shift")}


def _resolve_settings(spec: PreparationSpec) -> tuple[dict, dict, dict, dict, dict]:
    profile, data = spec.profile, spec.data
    overrides = _object(data.get("overrides", {}), {"incar", "kpoints", "potentials"}, "overrides")
    incar = _incar_settings(profile["incar"], "profile INCAR")
    origins = {"incar": {key: f"profile:{profile['name']}" for key in incar}}
    for key, value in _incar_settings(overrides.get("incar", {}), "override INCAR").items():
        incar[key] = value
        origins["incar"][key] = "case_override"
    invariants = {"NSW": 0, "IBRION": -1} if data["calculation"] == "static" else {"ISIF": 2}
    for key, value in invariants.items():
        if key in incar and incar[key] != value:
            raise PreparationError(f"{data['calculation']} invariant conflicts with {key}={incar[key]}; requires {value}")
        if key not in incar:
            origins["incar"][key] = "type_invariant"
            incar[key] = value
    required = {"GGA", "ENCUT", "PREC", "ISPIN", "ISMEAR", "SIGMA", "EDIFF", "NELM", "ALGO", "ISTART", "ICHARG"}
    if data["calculation"] == "fixed-cell-relaxation":
        required |= {"IBRION", "NSW", "EDIFFG"}
    if required - set(incar):
        raise PreparationError("Missing explicit scientific context: " + ", ".join(sorted(required - set(incar))))
    if any(key in incar for key in ("NCORE", "KPAR", "NPAR")):
        raise PreparationError("Parallelization belongs in runtime.incar (NCORE/KPAR); NPAR is outside this contract")
    if incar["ISTART"] != 0 or incar["ICHARG"] != 2:
        raise PreparationError("Only explicit fresh electronic initialization ISTART=0, ICHARG=2 is supported")
    if incar["ISPIN"] not in (1, 2):
        raise PreparationError("ISPIN must be 1 or 2")
    for key, supported in (("GGA", {"PE", "PS", "RP", "RE", "AM", "CA", "PZ"}),
                           ("PREC", {"NORMAL", "ACCURATE"}),
                           ("ALGO", {"NORMAL", "FAST", "VERYFAST"})):
        if incar[key] not in supported:
            raise PreparationError(f"Unsupported preparation {key}; choose explicitly from {', '.join(sorted(supported))}")
    if incar["ISMEAR"] not in {-5, -4, -1, 0, 1, 2}:
        raise PreparationError("Preparation supports ISMEAR=-5/-4/-1/0/1/2; occupations/smearing sweeps are outside scope")
    if "NELECT" in incar and incar["NELECT"] <= 0:
        raise PreparationError("Explicit NELECT must be positive")
    if incar["ISPIN"] == 2 and "MAGMOM" not in incar:
        raise PreparationError("Spin-polarized preparation requires explicit ordered MAGMOM")
    if incar.get("LDAU"):
        if not {"LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ"} <= set(incar) or incar["LDAUTYPE"] not in (1, 2):
            raise PreparationError("DFT+U requires explicit LDAUTYPE=1/2 and ordered LDAUL/LDAUU/LDAUJ")
    if incar.get("LDIPOL") and (incar.get("IDIPOL") not in (1, 2, 3, 4) or "DIPOL" not in incar):
        raise PreparationError("Dipole correction requires explicit IDIPOL=1/2/3/4 and DIPOL")
    if any(incar.get(key) for key in ("LHFCALC", "LNONCOLLINEAR", "LSORBIT", "LUSE_VDW")) or "METAGGA" in incar:
        raise PreparationError("Hybrid, meta-GGA, SOC/noncollinear and nonlocal-vdW modes are outside preparation MVP")
    if any(key in incar for key in ("AEXX", "HFSCREEN", "NKRED", "NKREDX", "NKREDY", "NKREDZ", "IALGO")):
        raise PreparationError("Hybrid-specific settings and alternate IALGO selection are outside preparation MVP")
    if data["calculation"] == "fixed-cell-relaxation":
        if incar["IBRION"] not in (1, 2, 3) or incar["NSW"] <= 0 or incar["EDIFFG"] == 0:
            raise PreparationError("Fixed-cell relaxation requires IBRION=1/2/3, NSW>0 and nonzero EDIFFG")
        if "POTIM" not in incar or incar["POTIM"] <= 0:
            raise PreparationError("Relaxation requires explicit positive POTIM")
    kpoints = dict(_object(profile["kpoints"], {"mode", "mesh", "shift"}, "profile kpoints"))
    origins["kpoints"] = {key: f"profile:{profile['name']}" for key in kpoints}
    for key, value in _object(overrides.get("kpoints", {}), {"mode", "mesh", "shift"}, "override kpoints").items():
        kpoints[key] = value
        origins["kpoints"][key] = "case_override"
    potentials = dict(_object(profile["potentials"], {"root", "variants", "requirements", "library_release"}, "profile potentials"))
    origins["potentials"] = {key: f"profile:{profile['name']}" for key in potentials}
    pot_overrides = _object(overrides.get("potentials", {}), {"root", "variants", "requirements", "library_release"}, "override potentials")
    for key, value in pot_overrides.items():
        potentials[key] = value
        origins["potentials"][key] = "case_override"
    if not {"root", "variants", "requirements"} <= set(potentials):
        raise PreparationError("Explicit potential root, variants and comparability requirements are required")
    if not isinstance(potentials["requirements"], dict) or not potentials["requirements"].get("family"):
        raise PreparationError("Potential requirements must explicitly name a family")
    potentials["root"] = str(_source(potentials["root"], spec.path.parent if "root" in pot_overrides else spec.profile_base))
    runtime = _object(data.get("runtime", {}), {"mpi_ranks", "threads_per_rank", "cpus", "incar"}, "runtime")
    for key in ("mpi_ranks", "threads_per_rank", "cpus"):
        if key in runtime and (type(runtime[key]) is not int or runtime[key] <= 0):
            raise PreparationError(f"runtime.{key} must be a positive integer")
    overlay = _incar_settings(runtime.get("incar", {}), "runtime INCAR")
    if set(overlay) - {"NCORE", "KPAR"} or any(value <= 0 for value in overlay.values()):
        raise PreparationError("Declared runtime INCAR overlay supports positive NCORE/KPAR only")
    ranks = runtime.get("mpi_ranks")
    if ranks and all(key in runtime for key in ("cpus", "threads_per_rank")) and ranks * runtime["threads_per_rank"] > runtime["cpus"]:
        raise PreparationError("Declared MPI ranks times threads exceeds declared CPUs")
    if ranks and overlay and ranks % (overlay.get("NCORE", 1) * overlay.get("KPAR", 1)):
        raise PreparationError("Declared NCORE*KPAR must divide declared MPI ranks")
    runtime = {**runtime, "incar": overlay}
    origins["runtime"] = {key: "declared_runtime_overlay" for key in runtime if key != "incar"}
    origins["runtime"]["incar"] = {key: "declared_runtime_overlay" for key in overlay}
    return incar, kpoints, potentials, runtime, origins


def _numeric_equal(left: object, right: object) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_numeric_equal(a, b) for a, b in zip(left, right))
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isclose(left, right, rel_tol=0, abs_tol=1e-10)
    return left == right


def compare_inputs(baseline: Mapping[str, bytes], prepared: Mapping[str, bytes], *,
                   baseline_runtime: dict | None = None, prepared_runtime: dict | None = None) -> dict:
    """Compare supported semantics; exact bytes remain alongside every judgment."""
    old, new = check_input_bytes(baseline), check_input_bytes(prepared)
    complete = all(check["status"] == "valid" and not check["inputs"].get("INCAR", {}).get("unknown_tags") for check in (old, new))
    changes = []
    if complete:
        a, b = old["inputs"], new["inputs"]
        for key, category in (("cell", "cell"), ("cartesian_coordinates", "coordinates"),
                              ("species", "species_blocks"), ("counts", "species_blocks"),
                              ("selective_dynamics", "constraints")):
            equal = _numeric_equal(a["POSCAR"].get(key), b["POSCAR"].get(key)) if key in ("cell", "cartesian_coordinates") else a["POSCAR"].get(key) == b["POSCAR"].get(key)
            if not equal:
                changes.append({"category": category, "field": key, "before": a["POSCAR"].get(key), "after": b["POSCAR"].get(key)})
        before, after = a["INCAR"]["settings"], b["INCAR"]["settings"]
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                category = "title_comment_format" if key == "SYSTEM" else "runtime_parallelization" if key in {"NCORE", "KPAR", "NPAR"} else "scientific_incar"
                changes.append({"category": category, "field": key, "before": before.get(key), "after": after.get(key)})
        for key in ("mode", "mesh", "shift"):
            if a["KPOINTS"][key] != b["KPOINTS"][key]:
                changes.append({"category": "kpoint_sampling", "field": key, "before": a["KPOINTS"][key], "after": b["KPOINTS"][key]})
        if baseline["POTCAR"] != prepared["POTCAR"]:
            changes.append({"category": "potential_content", "before": a["POTCAR"]["datasets"], "after": b["POTCAR"]["datasets"]})
        for filename, categories in (("POSCAR", {"cell", "coordinates", "species_blocks", "constraints"}),
                                     ("INCAR", {"scientific_incar", "runtime_parallelization", "title_comment_format"}),
                                     ("KPOINTS", {"kpoint_sampling"})):
            if baseline[filename] != prepared[filename] and not any(item["category"] in categories for item in changes):
                changes.append({"category": "title_comment_format", "file": filename})
    else:
        changes.append({"category": "missing_evidence", "message": "One or more input dialects or tags cannot be compared completely"})
    runtime_assessed = baseline_runtime is not None
    if not runtime_assessed:
        changes.append({"category": "missing_evidence", "field": "expected_runtime", "message": "Baseline bundle alone does not establish a declared runtime overlay"})
    elif baseline_runtime != prepared_runtime:
        changes.append({"category": "runtime_parallelization", "field": "expected_runtime", "before": baseline_runtime, "after": prepared_runtime})
    scientific_tags = [item["field"] for item in changes if item["category"] == "scientific_incar"]
    only_tags = scientific_tags if complete and runtime_assessed and scientific_tags and all(item["category"] in {"scientific_incar", "title_comment_format"} for item in changes) else None
    return {"scope": "supported prepared inputs and declared runtime; not effective execution",
            "complete": complete and runtime_assessed, "prepared_input_comparison_complete": complete,
            "numeric_absolute_tolerance_angstrom": 1e-10, "numeric_relative_tolerance": 0,
            "baseline_identities": old["identities"], "prepared_identities": new["identities"],
            "changes": changes, "only_incar_tags_changed": only_tags,
            "baseline_findings": old["findings"]}


def _baseline(value: object, base: Path, files: dict, runtime: dict) -> tuple[dict, list[dict], list[Path]]:
    sources, protected = [], []
    declared_runtime = None
    consistency = None
    recorded_identities = None
    if isinstance(value, dict):
        _object(value, {"record"}, "baseline", {"record"})
        path = _source(value["record"], base)
        raw = path.read_bytes()
        record = _decode(raw)
        if record.get("record_kind") != "vasp-input-preparation" or record.get("schema_version") != 1:
            raise PreparationError("Baseline record must be a schema-1 VASP input preparation")
        publication = record.get("publication")
        if not isinstance(publication, dict) or not isinstance(publication.get("input_path"), str):
            raise PreparationError("Baseline record requires a publication input_path locator")
        recorded_identities = record.get("prepared_inputs")
        if not isinstance(recorded_identities, dict) or not isinstance(record.get("expected_runtime"), dict):
            raise PreparationError("Baseline record requires prepared-input identities and expected runtime evidence")
        consistency = inspect_publication(path)
        declared_runtime = record.get("expected_runtime")
        sources.append(_source_record("baseline_record", value["record"], path, raw))
        protected.append(path)
        directory = _source(publication["input_path"], path.parent)
    else:
        directory = _source(value, base)
    if not directory.is_dir():
        raise PreparationError("Baseline must locate an input directory")
    protected.append(directory)
    old = {}
    for name in INPUT_NAMES:
        path = directory / name
        if path.exists():
            path = _source(path, base)
            old[name] = path.read_bytes()
            sources.append(_source_record("baseline_input", path, path, old[name]))
    result = compare_inputs(old, files, baseline_runtime=declared_runtime, prepared_runtime=runtime)
    extras = sorted(path.name for path in directory.iterdir() if path.name not in INPUT_NAMES)
    snapshot_mismatch = recorded_identities is not None and recorded_identities != {name: content_identity(data) for name, data in old.items()}
    if extras or snapshot_mismatch or (consistency is not None and consistency["status"] != "valid"):
        result.update(complete=False, prepared_input_comparison_complete=False, only_incar_tags_changed=None)
        result["changes"].append({"category": "missing_evidence", "extra_entries": extras,
                                  "record_snapshot_mismatch": snapshot_mismatch, "record_consistency": consistency})
    return result, sources, protected


def prepare(spec_path: str | Path, *, output: str | Path, scratch_root: str | Path,
            record_directory: str | Path, scratch_mount: str | Path | None = None,
            dry_run: bool = False) -> dict:
    """Resolve actual source snapshots anew and publish only validated candidates."""
    spec = load_spec(spec_path)
    incar, mesh, potentials, runtime, origins = _resolve_settings(spec)
    source = _source(spec.data["structure"], spec.path.parent)
    poscar = source.read_bytes()
    structure = parse_poscar(poscar)
    if structure["status"] != "valid":
        raise PreparationError("Prepared POSCAR is malformed or outside supported dialect", findings=structure["findings"])
    variants = potentials["variants"]
    if not isinstance(variants, dict) or set(variants) != set(structure["species"]) or any(not isinstance(value, str) for value in variants.values()):
        raise PreparationError("Potential variants must explicitly map every distinct POSCAR element, with no extra elements")
    selection = resolve_potcar(poscar, root=potentials["root"], pots=[f"{key}={value}" for key, value in variants.items()],
                              requirements=potentials["requirements"], library_release=potentials.get("library_release"), strict=True)
    kpoints, resolved_mesh = _kpoints(mesh)
    files = {"INCAR": _incar_text(incar), "KPOINTS": kpoints, "POSCAR": poscar, "POTCAR": selection.content}
    validation = check_input_bytes(files, expected_selection=selection.record)
    if validation["status"] != "valid":
        raise PreparationError("Candidate input validation failed", findings=validation["findings"])
    sources = list(spec.sources) + [_source_record("prepared_structure_source", spec.data["structure"], source, poscar)]
    protected = [Path(item["resolved_location"]) for item in sources] + [Path(potentials["root"]), *selection.source_paths]
    for path in selection.source_paths:
        block = next(item for item in selection.record["blocks"] if item["source_path"] == str(path))
        sources.append({"role": "selected_potential", "recorded_location": str(path), "resolved_location": str(path), "sha256": block["source_sha256"]})
    if "source_record" in spec.data:
        path = _source(spec.data["source_record"], spec.path.parent)
        raw = path.read_bytes()
        lineage = _decode(raw)
        if lineage.get("record_kind") != "molecular-embedding" or inspect_publication(path, input_path=source)["status"] != "valid":
            raise PreparationError("source_record must be a complete matching molecular embedding record")
        identities = list(lineage.get("prepared_inputs", {}).values())
        if identities != [content_identity(poscar)]:
            raise PreparationError("Embedding record differs from the accepted source POSCAR bytes")
        sources.append(_source_record("embedding_record", spec.data["source_record"], path, raw))
        protected.append(path)
    differences = None
    if "baseline" in spec.data:
        differences, extra_sources, extra_protected = _baseline(spec.data["baseline"], spec.path.parent, files, runtime)
        sources.extend(extra_sources)
        protected.extend(extra_protected)
    plan = publication_plan(output=output, scratch_root=scratch_root, record_directory=record_directory,
                            scratch_mount=scratch_mount, sources=protected)
    identities = {name: content_identity(data) for name, data in files.items()}
    summary = {key: structure[key] for key in ("species", "counts", "atom_count", "cell", "coordinate_mode", "selective_dynamics")}
    record = {"schema_version": 1, "record_kind": "vasp-input-preparation",
              "preparation_content_id": stable_hash({"calculation": spec.data["calculation"], "prepared_inputs": identities, "expected_runtime": runtime}),
              "scientific_target_id": None,
              "sources": sources, "calculation": spec.data["calculation"],
              "profile": spec.profile, "overrides": spec.data.get("overrides", {}),
              "resolved_settings": {"incar": incar, "kpoints": resolved_mesh, "potentials": potentials},
              "settings_origins": origins, "structure": summary, "atom_mapping": "identity; source POSCAR bytes preserved",
              "selected_potentials": selection.record, "prepared_inputs": identities,
              "validation": {key: validation[key] for key in ("status", "findings")},
              "differences": differences, "expected_runtime": runtime,
              "effective_inputs": {"status": "unobserved", "evidence": None},
              "renderer": {"cmw_version": _cmw_version(), "preparation_schema": 1, "input_parser": "cmw-bounded-v1"}}
    if spec.data["calculation"] == "fixed-cell-relaxation":
        record["stopping_convention"] = "force (eV/Angstrom)" if incar["EDIFFG"] < 0 else "energy (eV)"
    if dry_run:
        return {**record, "dry_run": True, "publication": {"state": "preview", "input_path": plan["input_path"], "record_path": plan["record_path"]},
                "publication_plan": plan}
    return publish_preparation(files, record, plan)
