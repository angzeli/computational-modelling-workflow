"""Explicit declarative acceptance of bounded VASP result evidence.

A policy is scientific intent supplied by the caller. It neither creates native
facts nor proves execution provenance; finalization applies its separate binding
and publication gates even when a diagnostic policy omits those requirements.
"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path

from cmw.core.provenance import canonical_json_bytes, stable_hash
from .result_sources import read_json_source
from .results import CELL_TOLERANCE, POSITION_TOLERANCE, compare_geometry


ARTIFACT_ROLES = frozenset({
    "energy:free_energy", "energy:without_entropy", "energy:sigma_to_zero",
    "periodic-structure",
})


def _finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _keys(value: object, expected: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} requires exactly these keys: {', '.join(sorted(expected))}")
    return value


def validate_policy(value: dict) -> dict:
    """Validate schema 1 without scientific defaults or executable expressions.

    The returned independent dictionary contains only the policy content. Its
    semantic content identity is ``stable_hash(policy)``; source-byte identity
    is separately supplied by ``load_policy``.
    """
    policy = _keys(value, {
        "schema_version", "name", "calculation", "electronic", "force", "endpoint",
        "require_execution", "require_input_binding", "artifact_roles",
    }, "Acceptance policy")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("Unsupported acceptance policy schema_version")
    if not isinstance(policy["name"], str) or not policy["name"].strip():
        raise ValueError("Policy name must be nonempty text")
    if policy["calculation"] not in ("static", "fixed-cell-relaxation"):
        raise ValueError("Policy calculation must be static or fixed-cell-relaxation")
    electronic = _keys(policy["electronic"], {"history", "basis", "criterion"}, "Electronic policy")
    if electronic["history"] not in ("all", "final"):
        raise ValueError("Electronic history must be all or final")
    if electronic["basis"] not in ("native", "numeric", "native-and-numeric"):
        raise ValueError("Electronic basis must be native, numeric or native-and-numeric")
    if electronic["criterion"] != "run-ediff":
        raise ValueError("Only the explicit run-ediff electronic criterion is supported")
    endpoint = _keys(policy["endpoint"], {"position_tolerance_angstrom", "cell_tolerance_angstrom"}, "Endpoint policy")
    for key, maximum in (("position_tolerance_angstrom", POSITION_TOLERANCE),
                         ("cell_tolerance_angstrom", CELL_TOLERANCE)):
        if not _finite(endpoint[key]) or not 0 < endpoint[key] <= maximum:
            raise ValueError(f"{key} must be positive and no larger than the parser tolerance {maximum:g}")
    for key in ("require_execution", "require_input_binding"):
        if type(policy[key]) is not bool:
            raise ValueError(f"{key} must be an explicit JSON boolean")
    roles = policy["artifact_roles"]
    if (not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles)
            or len(set(roles)) != len(roles) or not set(roles) <= ARTIFACT_ROLES):
        raise ValueError("artifact_roles must be a nonempty unique list of supported roles")
    force = policy["force"]
    if policy["calculation"] == "static":
        if force is not None or "periodic-structure" in roles:
            raise ValueError("Static policies require force=null and electronic energy roles only")
    else:
        if not isinstance(force, dict):
            raise ValueError("Fixed-cell relaxation requires an explicit force policy")
        criterion = force.get("criterion")
        expected = {"criterion", "scope"}
        if criterion == "explicit":
            expected.add("threshold_ev_per_angstrom")
        elif criterion != "run-ediffg":
            raise ValueError("Force criterion must be run-ediffg or explicit")
        _keys(force, expected, "Force policy")
        if force["scope"] not in ("all", "free"):
            raise ValueError("Force scope must be all or free")
        if criterion == "explicit" and (not _finite(force["threshold_ev_per_angstrom"])
                                         or force["threshold_ev_per_angstrom"] <= 0):
            raise ValueError("Explicit force threshold must be positive finite eV/Angstrom")
    # Reject nonfinite JSON anywhere, including unexpected Python-only objects.
    canonical_json_bytes(policy)
    return deepcopy(policy)


def load_policy(path: Path | str) -> tuple[dict, dict]:
    """Read a strict, bounded JSON policy and retain its exact source observation."""
    value, source = read_json_source(path, "acceptance_policy")
    return validate_policy(value), source


def _truth(value: object) -> str:
    return "PASS" if value is True else "FAIL" if value is False else "UNKNOWN"


def _file_geometry(parsed: object) -> dict | None:
    if not isinstance(parsed, dict) or parsed.get("status") != "valid":
        return None
    return {"cell": parsed["cell"], "species": parsed["atom_species"],
            "cartesian": parsed["cartesian_coordinates"]}


def assess_result(evidence: dict, policy: dict, *, binding: dict | None = None,
                  execution: dict | None = None) -> dict:
    """Assess immutable observed facts under one explicit caller-selected policy.

    An unbound diagnostic policy may accept its scientific evidence if both
    provenance requirements are false. Such acceptance cannot finalize artifacts.
    Whole-atom constrained relaxation always requires verified input binding.
    """
    policy = validate_policy(policy)
    checks = []

    def check(code: str, status: str, details: object = None) -> None:
        checks.append({"code": code, "status": status, "evidence": deepcopy(details)})

    def truth(code: str, value: object, details: object = None) -> None:
        check(code, _truth(value), details)

    def finish() -> dict:
        statuses = {item["status"] for item in checks}
        status = next((item for item in ("FAIL", "UNSUPPORTED", "UNKNOWN") if item in statuses), "PASS")
        return {
            "schema_version": 1,
            "status": status,
            "exit_code": 0 if status == "PASS" else 1 if status == "FAIL" else 2,
            "policy_identity": {"schema_version": 1, "name": policy["name"], "policy_id": stable_hash(policy)},
            "snapshot_id": evidence.get("snapshot_id"),
            "segment_id": (evidence.get("selected_segment") or {}).get("segment_id"),
            "artifact_roles": policy["artifact_roles"],
            "checks": checks,
            "interpretation": f"Accepted under policy {policy['name']}, within its stated scope." if status == "PASS" else "Acceptance is not established under the selected policy.",
            "limitations": [
                "Policy acceptance alone does not finalize artifacts or establish execution binding.",
                "Physical-model accuracy, cutoff/k-point convergence, minimum character and magnetic ground state are not assessed.",
            ],
        }

    state = evidence.get("status")
    check("evidence.readable", "FAIL" if state == "invalid" else "UNSUPPORTED" if state == "unsupported"
          else "PASS" if state == "inspected" else "UNKNOWN", {"status": state, "error": evidence.get("error")})
    current = evidence.get("selected_segment")
    if not isinstance(current, dict):
        check("segment.selected", "UNKNOWN", evidence.get("segments"))
        return finish()
    truth("segment.identity", bool(current.get("segment_id")) and bool(evidence.get("snapshot_id")))
    coverage = evidence.get("coverage", {})
    check("sources.complete", "PASS" if coverage.get("all_sources_complete") is True and evidence.get("sources") else "UNKNOWN", coverage)
    truth("sources.stable", coverage.get("all_sources_stable"), coverage)
    conflicts = evidence.get("conflicts", []) + current.get("conflicts", [])
    truth("evidence.coherent", not conflicts, conflicts)
    mode = evidence.get("mode", {})
    check("mode.supported", "PASS" if mode.get("supported") is True else "UNSUPPORTED", mode)
    check("mode.calculation", "UNSUPPORTED" if mode.get("calculation") == "unsupported"
          else "PASS" if mode.get("calculation") == policy["calculation"] else "FAIL", mode)
    evaluations = current.get("evaluations", [])
    check("evaluations.complete", "PASS" if evaluations and not current.get("trailing_incomplete")
          and all(item.get("complete") is True for item in evaluations) else "UNKNOWN",
          {"evaluation_count": len(evaluations), "trailing_incomplete": current.get("trailing_incomplete")})
    if policy["electronic"]["history"] == "all":
        check("electronic.full_history", "PASS" if coverage.get("full_electronic_history") is True else "UNKNOWN", coverage)
    termination = current.get("termination", {})
    check("termination.normal_footer", "PASS" if termination.get("normal_footer") is True else "UNKNOWN", termination.get("footer_reference"))
    truth("termination.no_fatal", not termination.get("fatal"), termination.get("fatal"))
    truth("termination.no_requested_stop", not termination.get("requested_stop"), termination.get("requested_stop"))
    if binding is not None:
        truth("binding.valid", binding.get("valid"), binding.get("findings"))
    if policy["require_execution"]:
        operational = (execution or {}).get("status")
        check("execution.completed", "PASS" if operational == "eligible" else "FAIL" if operational == "invalid" else "UNKNOWN",
              {"status": operational, "checks": (execution or {}).get("checks"), "exit_code": (execution or {}).get("exit_code")})
        truth("execution.binding", (binding or {}).get("valid"))

    selected_evaluations = evaluations if policy["electronic"]["history"] == "all" else evaluations[-1:]
    if not selected_evaluations:
        check("electronic.selected_evaluations", "UNKNOWN")
    for item in selected_evaluations:
        prefix = f"electronic.evaluation_{item.get('index')}"
        threshold = item.get("effective_ediff")
        details = {"evaluation_index": item.get("index"), "ediff": threshold,
                   "nelm": item.get("effective_nelm"), "budget_reached": item.get("budget_reached")}
        check(prefix + ".criterion", "PASS" if _finite(threshold) and threshold > 0
              else "UNSUPPORTED" if _finite(threshold) else "UNKNOWN", details)
        if policy["electronic"]["basis"] in ("native", "native-and-numeric"):
            truth(prefix + ".native", item.get("native_convergence"), item.get("native_convergence_reference"))
        if policy["electronic"]["basis"] in ("numeric", "native-and-numeric"):
            numeric = item.get("numeric_convergence")
            check(prefix + ".numeric", numeric if numeric in {"PASS", "FAIL"} else "UNSUPPORTED" if numeric == "NOT_APPLICABLE" else "UNKNOWN",
                  {"numeric_checks": item.get("numeric_checks"), "final_row": item.get("final_electronic_row")})

    endpoint = evidence.get("endpoint", {})
    geometry = endpoint.get("geometry")
    check("endpoint.associated", "PASS" if geometry and endpoint.get("is_last_complete_evaluation") is True else "UNKNOWN",
          {"evaluation_index": endpoint.get("evaluation_index"), "is_last_complete_evaluation": endpoint.get("is_last_complete_evaluation")})
    truth("endpoint.final_segment", endpoint.get("is_final_segment"))
    files = evidence.get("filesystem_inputs", {})
    tolerances = policy["endpoint"]

    def compare(code: str, first: dict | None, second: dict | None) -> None:
        try:
            comparison = compare_geometry(first, second,
                position_tolerance=tolerances["position_tolerance_angstrom"],
                cell_tolerance=tolerances["cell_tolerance_angstrom"])
        except (KeyError, TypeError, IndexError, ValueError):
            comparison = {"status": "unavailable", "reason": "geometry evidence is incomplete"}
        resolution = (first or {}).get("printed_resolution", {})
        position_precision = resolution.get("position_component_half_last_place_angstrom")
        cell_precision = resolution.get("cell_component_half_last_place_angstrom")
        if comparison["status"] == "match" and (
                _finite(position_precision) and tolerances["position_tolerance_angstrom"] < math.sqrt(3) * position_precision
                or _finite(cell_precision) and tolerances["cell_tolerance_angstrom"] < cell_precision):
            comparison.update(status="unavailable", reason="requested tolerance is below native printed resolution",
                              printed_resolution=deepcopy(resolution))
        check(code, {"match": "PASS", "mismatch": "FAIL", "unsupported": "UNSUPPORTED"}.get(comparison["status"], "UNKNOWN"), comparison)

    poscar = _file_geometry(files.get("POSCAR"))
    constraints = evidence.get("constraints", {})
    flags = constraints.get("flags")
    has_fixed = bool(flags and any(not any(row) for row in flags))
    binding_required = policy["require_input_binding"] or (policy["calculation"] == "fixed-cell-relaxation" and has_fixed)
    if binding_required:
        for key in ("valid", "input_binding", "input_comparison_valid"):
            truth("inputs." + key, (binding or {}).get(key), {"required_by_policy": policy["require_input_binding"], "whole_atom_constraints": has_fixed})
    if policy["calculation"] == "static":
        check("ionic.relaxation", "NOT_APPLICABLE", {"calculation": "static"})
        compare("endpoint.static_poscar", geometry, poscar)
    else:
        initial_positions = current.get("initial_positions")
        initial = None
        if initial_positions and initial_positions.get("cell") and geometry:
            initial = {"cell": initial_positions["cell"]["matrix"],
                       "cartesian": initial_positions["cartesian"], "species": geometry["species"],
                       "printed_resolution": {
                           "position_component_half_last_place_angstrom": initial_positions.get("position_half_last_place"),
                           "cell_component_half_last_place_angstrom": initial_positions["cell"].get("component_half_last_place"),
                       }}
        compare("endpoint.initial_poscar", initial, poscar)
        compare("endpoint.contcar", geometry, _file_geometry(files.get("CONTCAR")))
        difference = endpoint.get("comparisons", {}).get("fixed_cell", {}).get("max_cell_difference_angstrom")
        check("endpoint.fixed_cell", "PASS" if _finite(difference) and difference <= tolerances["cell_tolerance_angstrom"]
              else "FAIL" if _finite(difference) else "UNKNOWN", {"maximum_difference_angstrom": difference, "tolerance_angstrom": tolerances["cell_tolerance_angstrom"]})
        truth("constraints.endpoint_flags", constraints.get("contcar_flags_match"))
        check("constraints.whole_atoms", "UNSUPPORTED" if constraints.get("partial") else "PASS", flags)
        ionic = evidence.get("ionic", {})
        ediffg = ionic.get("ediffg")
        check("ionic.force_stopping_mode", "PASS" if _finite(ediffg) and ediffg < 0
              else "UNSUPPORTED" if _finite(ediffg) else "UNKNOWN", {"ediffg": ediffg, "criterion_kind": ionic.get("criterion_kind")})
        check("ionic.native_stop", "PASS" if ionic.get("native_stopping_reference") else "UNKNOWN", ionic.get("native_stopping_reference"))
        force = policy["force"]
        threshold = force.get("threshold_ev_per_angstrom") if force["criterion"] == "explicit" else -ediffg if _finite(ediffg) and ediffg < 0 else None
        summary = endpoint.get("force_summary") or {}
        all_atoms, free_atoms, fixed_atoms = (summary.get(group) or {} for group in ("all", "free", "fixed"))
        vectors = endpoint.get("force_vectors")
        count = len(vectors) if isinstance(vectors, list) else None
        counts = [group.get("count") for group in (all_atoms, free_atoms, fixed_atoms)]
        accounted = (type(count) is int and count > 0 and all(type(n) is int and n >= 0 for n in counts)
                     and counts[0] == count and counts[1] + counts[2] == count)
        check("forces.accounting", "PASS" if accounted else "UNSUPPORTED" if constraints.get("partial") else "UNKNOWN",
              {"all": all_atoms, "free": free_atoms, "fixed": fixed_atoms, "partial": summary.get("partial")})
        check("forces.nonempty_free_set", "PASS" if type(free_atoms.get("count")) is int and free_atoms["count"] > 0 else "UNKNOWN", free_atoms)
        group = summary.get(force["scope"]) or {}
        maximum, precision = group.get("maximum_norm"), endpoint.get("force_half_last_place")
        force_status = "UNKNOWN"
        lower = upper = None
        if (_finite(maximum) and maximum >= 0 and _finite(precision) and precision >= 0
                and _finite(threshold) and threshold > 0 and accounted and free_atoms["count"] > 0
                and not constraints.get("partial")):
            uncertainty = math.sqrt(3) * precision
            lower, upper = max(0.0, maximum - uncertainty), maximum + uncertainty
            force_status = "PASS" if upper < threshold else "FAIL" if lower >= threshold else "UNKNOWN"
        check("forces.threshold", force_status, {"scope": force["scope"], "criterion": force["criterion"],
              "threshold_ev_per_angstrom": threshold, "maximum_norm_ev_per_angstrom": maximum,
              "maximum_atom_index": group.get("maximum_atom_index"), "index_base": 0,
              "lower_bound": lower, "upper_bound": upper, "component_half_last_place": precision})

    final = evaluations[-1] if evaluations else {}
    for role in policy["artifact_roles"]:
        if role.startswith("energy:"):
            value = final.get("energies", {}).get(role.split(":", 1)[1]) or {}
            check("artifact." + role, "PASS" if _finite(value.get("value")) and value.get("units") == "eV" else "UNKNOWN", value)
        else:
            check("artifact.periodic-structure", "PASS" if geometry and geometry.get("geometry_id") else "UNKNOWN",
                  {"geometry_id": (geometry or {}).get("geometry_id")})
    return finish()


__all__ = ["ARTIFACT_ROLES", "validate_policy", "load_policy", "assess_result"]
