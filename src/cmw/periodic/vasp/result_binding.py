"""Explicit retrospective binding of native VASP and saved Jobs evidence.

A caller declaration associates exact observed content with an operational
attempt. It is not an authenticated pre-run input receipt or scientific target.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re

from .result_execution import inspect_execution
from .result_sources import SourceSnapshotError, read_json_source


ARTIFACT_ROLES = frozenset({"energy:free_energy", "energy:without_entropy",
                            "energy:sigma_to_zero", "periodic-structure"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED = {"schema_version", "intent", "producing_calculation", "snapshot_id", "segment_id",
             "policy_id", "source_identities", "execution", "artifact_roles"}
_OPTIONAL = {"parents", "expected_runtime_overlay", "preparation_record"}


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _path(value):
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def _identities(value):
    return (isinstance(value, dict) and bool(value)
            and all(isinstance(role, str) and bool(role) and _digest(identity)
                    for role, identity in value.items()))


def _same(first, second):
    # JSON booleans are not interchangeable with integer INCAR values.
    if isinstance(first, bool) or isinstance(second, bool):
        return type(first) is type(second) and first == second
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(_same(a, b) for a, b in zip(first, second))
    return first == second


def _validate_spec(value):
    if not isinstance(value, dict) or not _REQUIRED <= set(value) or set(value) - _REQUIRED - _OPTIONAL:
        raise ValueError("Finalization specification has missing or unknown fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Finalization specification requires schema_version 1")
    if value["intent"] != "retrospective":
        raise ValueError("Only explicitly retrospective finalization intent is supported")
    if not isinstance(value["producing_calculation"], str) or not value["producing_calculation"].strip():
        raise ValueError("producing_calculation must be a nonempty caller declaration")
    if not all(_digest(value[name]) for name in ("snapshot_id", "segment_id", "policy_id")):
        raise ValueError("Snapshot, segment and policy identities require lowercase SHA-256 values")
    if not _identities(value["source_identities"]):
        raise ValueError("source_identities must explicitly map every native source role to SHA-256")
    execution = value["execution"]
    if (not isinstance(execution, dict)
            or set(execution) != {"snapshot", "receipt", "job_id", "attempt_id", "source_identities"}
            or not all(_path(execution[name]) for name in ("snapshot", "receipt"))
            or type(execution["job_id"]) is not int or execution["job_id"] <= 0
            or not isinstance(execution["attempt_id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", execution["attempt_id"]) is None
            or not _identities(execution["source_identities"])
            or set(execution["source_identities"]) != {"jobs_snapshot", "jobs_receipt"}):
        raise ValueError("execution must select one exact native Jobs attempt and both source identities")
    roles = value["artifact_roles"]
    if (not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles)
            or len(set(roles)) != len(roles) or set(roles) - ARTIFACT_ROLES):
        raise ValueError("artifact_roles must uniquely select supported energy roles or periodic-structure")
    overlay = value.get("expected_runtime_overlay", {})
    if (not isinstance(overlay, dict) or set(overlay) - {"NCORE", "KPAR"}
            or any(type(count) is not int or count <= 0 for count in overlay.values())):
        raise ValueError("expected_runtime_overlay permits only explicit positive integer NCORE and KPAR")
    parents = value.get("parents", [])
    if not isinstance(parents, list):
        raise ValueError("parents must be a list of explicit artifact declarations")
    for parent in parents:
        if (not isinstance(parent, dict) or set(parent) != {"record", "sha256", "artifact_id", "relationship"}
                or not _path(parent["record"]) or not _digest(parent["sha256"])
                or not _digest(parent["artifact_id"]) or parent["relationship"] != "input-structure"):
            raise ValueError("Each parent requires an exact record identity, artifact ID and input-structure relationship")
    if len({parent["artifact_id"] for parent in parents}) != len(parents):
        raise ValueError("Duplicate parent artifact declarations are unsupported")
    if "preparation_record" in value:
        preparation = value["preparation_record"]
        if (not isinstance(preparation, dict) or set(preparation) != {"path", "sha256"}
                or not _path(preparation["path"]) or not _digest(preparation["sha256"])):
            raise ValueError("preparation_record requires an explicit path and exact SHA-256")
    return value


def _resolved_spec(spec, base):
    resolved = copy.deepcopy(spec)

    def absolute(value):
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else base / path)

    for field in ("snapshot", "receipt"):
        resolved["execution"][field] = absolute(resolved["execution"][field])
    for parent in resolved.get("parents", []):
        parent["record"] = absolute(parent["record"])
    if "preparation_record" in resolved:
        resolved["preparation_record"]["path"] = absolute(resolved["preparation_record"]["path"])
    return resolved


def inspect_binding(spec_path, evidence, policy_id):
    """Validate one declaration against already parsed native evidence, read-only.

    Parent declaration syntax is checked here; the artifact finalizer must verify
    the referenced parent records and required core artifact relationships.
    No POTCAR, KPOINTS, queue database or source output is read by this adapter.
    """
    result = {"schema_version": 1, "kind": "vasp-retrospective-binding", "valid": False,
              "input_binding": False, "input_comparison_valid": False, "execution": None,
              "spec": None, "resolved_spec": None, "sources": [], "findings": [], "checks": {},
              "input_comparisons": [], "preparation": None,
              "limitations": [
                  "Retrospective caller declaration binds exact local evidence, not authenticated pre-run inputs.",
                  "Jobs evidence does not independently record scientific input or output identities.",
                  "Uncaptured effective settings, potential payloads and k-point comparability remain unassessed.",
                  "Parent declarations require separate artifact-record verification before finalization.",
              ]}

    def check(name, passed, message):
        result["checks"][name] = passed is True
        if passed is not True:
            result["findings"].append({"code": "binding." + name, "severity": "error",
                                       "scope": "binding", "message": message})

    try:
        raw, source = read_json_source(spec_path, "finalization_spec")
        result["sources"].append(source)
        spec = _validate_spec(raw)
    except (SourceSnapshotError, ValueError, TypeError) as exc:
        if isinstance(exc, SourceSnapshotError):
            result["sources"].append(exc.record)
        check("specification", False, str(exc))
        return result
    result["spec"] = spec
    resolved = _resolved_spec(spec, Path(source["absolute_path"]).parent)
    result["resolved_spec"] = resolved
    selected = evidence.get("selected_segment") or {}
    check("snapshot_identity", spec["snapshot_id"] == evidence.get("snapshot_id"),
          "Declared snapshot identity does not match inspected native content")
    check("segment_identity", spec["segment_id"] == selected.get("segment_id"),
          "Declared segment identity does not match the explicitly selected segment")
    check("policy_identity", spec["policy_id"] == policy_id,
          "Declared policy identity does not match the supplied acceptance policy")
    native = evidence.get("sources", [])
    actual_sources = {item.get("role"): item.get("sha256") for item in native}
    exact_sources = (bool(native) and len(actual_sources) == len(native)
                     and actual_sources == spec["source_identities"])
    check("source_identities", exact_sources,
          "Native source declarations must exhaustively match every inspected role and exact hash")
    complete = bool(native) and all(item.get("stable") is True and item.get("coverage") == "complete"
                                   and item.get("error") is None for item in native)
    check("source_coverage", complete, "Native sources must all be stable and completely observed")
    run_directory = evidence.get("run_directory")
    check("run_directory", _path(run_directory), "Inspected run directory is missing")
    if _path(run_directory):
        declared = resolved["execution"]
        execution = inspect_execution(declared["snapshot"], declared["receipt"],
                                      job_id=declared["job_id"], attempt_id=declared["attempt_id"],
                                      run_directory=run_directory)
        result["execution"] = execution
        result["sources"].extend(execution["sources"])
        result["findings"].extend(execution["findings"])
        observed = {item["role"]: item["sha256"] for item in execution["sources"]}
        check("execution_source_identities", observed == declared["source_identities"],
              "Saved Jobs snapshot or receipt content differs from its declared exact identity")
        check("execution", execution["status"] == "eligible",
              "Saved native Jobs evidence does not establish successful terminal completion")

    parsed = evidence.get("filesystem_inputs", {})
    poscar = parsed.get("POSCAR", {})
    incar = parsed.get("INCAR", {})
    check("input_sources", {"POSCAR", "INCAR"} <= set(actual_sources),
          "Both POSCAR and INCAR exact source identities are required")
    check("input_parsing", poscar.get("status") == "valid" and incar.get("status") == "valid",
          "Current POSCAR and INCAR must be usable under the supported parser contract")
    initial = evidence.get("endpoint", {}).get("comparisons", {}).get("initial_poscar", {})
    check("initial_geometry", initial.get("status") == "match",
          "Native initial positions, cell and species must match the bound POSCAR")
    constraints = evidence.get("constraints", {})
    flags = poscar.get("selective_dynamics")
    constraints_ok = (constraints.get("source_role") == "POSCAR" and constraints.get("flags") == flags
                      and (flags is None or isinstance(flags, list) and len(flags) == poscar.get("atom_count")
                           and all(isinstance(row, list) and len(row) == 3
                                   and all(type(flag) is bool for flag in row) for row in flags)))
    check("constraints", constraints_ok, "Bound selective-dynamics flags must be complete and retain atom order")
    input_checks = ("snapshot_identity", "segment_identity", "source_identities", "source_coverage",
                    "input_sources", "input_parsing", "initial_geometry", "constraints")
    result["input_binding"] = all(result["checks"][name] for name in input_checks)

    filesystem = incar.get("settings", {})
    effective = {tag: record.get("value") for tag, record in selected.get("settings", {}).items()}
    overlay = spec.get("expected_runtime_overlay", {})
    differences_valid = True
    for tag in sorted(set(filesystem) | set(overlay)):
        current, observed = filesystem.get(tag), effective.get(tag)
        status = "unassessed" if tag not in effective else "match" if _same(current, observed) else "conflict"
        if tag in overlay:
            expected = overlay[tag]
            allowed = tag in effective and type(observed) is int and observed == expected
            if allowed and not _same(current, observed):
                status = "declared_runtime_overlay"
            if not allowed:
                status = "conflict"
        if status == "conflict":
            differences_valid = False
        result["input_comparisons"].append({"tag": tag, "filesystem": current, "effective": observed,
                                             "expected_runtime": overlay.get(tag), "status": status})
    check("input_comparison", differences_valid,
          "Filesystem and observed effective settings differ without an exact supported runtime declaration")
    result["input_comparison_valid"] = differences_valid

    if "preparation_record" in resolved:
        declaration = resolved["preparation_record"]
        try:
            preparation, prep_source = read_json_source(declaration["path"], "preparation_record")
            result["sources"].append(prep_source)
            check("preparation_identity", prep_source["sha256"] == declaration["sha256"],
                  "Preparation record does not match its declared exact content identity")
            valid_shape = (isinstance(preparation, dict) and type(preparation.get("schema_version")) is int
                           and preparation["schema_version"] == 1
                           and preparation.get("record_kind") == "vasp-input-preparation"
                           and isinstance(preparation.get("publication"), dict)
                           and preparation["publication"].get("state") == "complete"
                           and isinstance(preparation.get("prepared_inputs"), dict)
                           and isinstance(preparation.get("resolved_settings"), dict)
                           and isinstance(preparation["resolved_settings"].get("incar"), dict))
            check("preparation_schema", valid_shape, "Preparation evidence requires a completed schema-1 VASP input record")
            if valid_shape:
                prepared_poscar = preparation["prepared_inputs"].get("POSCAR", {})
                check("preparation_poscar", isinstance(prepared_poscar, dict)
                      and _digest(prepared_poscar.get("sha256"))
                      and prepared_poscar["sha256"] == actual_sources.get("POSCAR"),
                      "Prepared POSCAR content must exactly match the bound input source")
                prepared = preparation["resolved_settings"]["incar"]
                comparisons = []
                valid_comparison = True
                for tag in sorted(set(prepared) | set(filesystem)):
                    declared, current = prepared.get(tag), filesystem.get(tag)
                    match = tag in prepared and tag in filesystem and _same(declared, current)
                    runtime = (tag in overlay and current == overlay[tag] and type(current) is int
                               and effective.get(tag) == overlay[tag])
                    status = "match" if match else "declared_runtime_overlay" if runtime else "conflict"
                    valid_comparison &= status != "conflict"
                    comparisons.append({"tag": tag, "prepared": declared, "filesystem": current,
                                        "effective": effective.get(tag), "status": status})
                check("preparation_incar", valid_comparison,
                      "Prepared and current INCAR settings differ outside the explicitly declared runtime overlay")
                result["preparation"] = {"source": prep_source, "record_kind": preparation["record_kind"],
                                         "preparation_content_id": preparation.get("preparation_content_id"),
                                         "input_path": preparation["publication"].get("input_path"),
                                         "comparisons": comparisons, "potentials": "unassessed", "kpoints": "unassessed"}
        except (SourceSnapshotError, ValueError, TypeError) as exc:
            if isinstance(exc, SourceSnapshotError):
                result["sources"].append(exc.record)
            check("preparation_record", False, str(exc))
    result["valid"] = bool(result["checks"]) and all(result["checks"].values())
    return result
