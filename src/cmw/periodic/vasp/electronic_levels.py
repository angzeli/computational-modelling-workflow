"""Independent electronic-level getters with endpoint and reference safeguards."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .analysis_context import AnalysisContext, AnalysisError, metric_call
from .results import _same_printed, compare_geometry

__all__ = ["AnalysisContext", "get_fermi_level", "get_vbm", "get_cbm", "get_band_gap",
           "get_vacuum_level", "get_work_function", "get_ionization_energy", "get_electron_affinity"]


def _status(*statuses: str) -> str:
    return next((s for s in ("FAIL", "UNSUPPORTED", "UNKNOWN", "NOT_APPLICABLE") if s in statuses), "PASS")


def _publish(ctx, metric, value, status, *, convention, findings=(), details=None, derived=False):
    gate, gates = ctx.final_gate()
    assessment = _status(status, gate)
    visible = value if assessment == "PASS" or (ctx.diagnostic and not derived) else None
    result = ctx.result(metric, visible, status=assessment, units="eV", convention=convention,
                        observed=value if assessment != "PASS" else None, findings=[*findings, *gates])
    if details is not None:
        result["details"] = details
    return result


def _xml(ctx):
    from .analysis_spectra import read_xml
    return ctx.read("xml", "vasprun.xml", lambda p: read_xml(p, evaluation_index=ctx.selected()["index"]))


def _native_geometry(ctx):
    segment, evaluation = ctx.native(), ctx.selected()
    geometry = evaluation.get("geometry")
    species = [s for s, count in zip(segment["species"], segment["counts"]) for _ in range(count)]
    if geometry is None or not geometry["cell"] or len(species) != len(geometry["cartesian"]):
        return None
    return {"cell": geometry["cell"]["matrix"], "cartesian": geometry["cartesian"], "species": species}


def _xml_correspondence(ctx, data):
    findings = list(data["findings"])
    calculation = data["selected_calculation"]
    if not data["complete"] or calculation is None or not calculation["complete"]:
        return "UNKNOWN", [*findings, {"code": "incomplete_xml"}]
    evaluation = ctx.selected()
    if data["version"] != ctx.native()["version"]:
        findings.append({"code": "xml_version_conflict"})
    if len(data["calculations"]) != len(ctx.native()["evaluations"]) or calculation["index"] != evaluation["index"]:
        findings.append({"code": "xml_evaluation_count_conflict"})
    if calculation["scstep_count"] != len(evaluation["iterations"]):
        findings.append({"code": "xml_iteration_count_conflict"})
    for key, value in ctx.settings().items():
        if key in data["settings"] and key not in {"EFERMI", "NCORE", "KPAR", "NPAR"}:
            other = data["settings"][key]
            if (str(value).upper() != str(other).upper()
                    and not (type(value) in (int, float) and type(other) in (int, float) and math.isclose(value, other, rel_tol=1e-8, abs_tol=1e-12))):
                findings.append({"code": "xml_setting_conflict", "tag": key, "native": value, "xml": other})
    if not _same_printed(evaluation["energies"].get("free_energy"), calculation["energies"].get("e_fr_energy")):
        findings.append({"code": "xml_evaluated_energy_conflict"})
    for native, step in zip(evaluation["iterations"], calculation.get("scsteps", [])):
        if not _same_printed(native["electronic_free_energy"], step["energies"].get("e_fr_energy")):
            findings.append({"code": "xml_electronic_energy_conflict", "label": native["label"]})
            break
    geometry = calculation["geometry"]
    if geometry and geometry.get("cell") and geometry.get("fractional") and geometry.get("elements"):
        cell = np.asarray(geometry["cell"], dtype=float)
        fractional = np.asarray(geometry["fractional"], dtype=float)
        if (cell.shape != (3, 3) or fractional.shape != (len(geometry["elements"]), 3)
                or not np.isfinite(cell).all() or not np.isfinite(fractional).all()):
            return "FAIL", [*findings, {"code": "invalid_xml_geometry_dimensions"}]
        with np.errstate(over="ignore", invalid="ignore"):
            cartesian = fractional @ cell
        if not np.isfinite(cartesian).all():
            return "FAIL", [*findings, {"code": "nonfinite_xml_cartesian_geometry"}]
        xml_geometry = {"cell": geometry["cell"], "cartesian": cartesian.tolist(), "species": geometry["elements"]}
        comparison = compare_geometry(_native_geometry(ctx), xml_geometry)
        if comparison["status"] == "unavailable":
            return "UNKNOWN", [*findings, {"code": "native_geometry_identity_unavailable"}]
        if comparison["status"] != "match":
            findings.append({"code": "xml_geometry_conflict", "comparison": comparison})
        else:
            cell_error = np.asarray([[v["half_last_place"] for v in row] for row in geometry["cell_tokens"]])
            fraction_error = np.asarray([[v["half_last_place"] for v in row] for row in geometry["fractional_tokens"]])
            cart_error = fraction_error @ abs(cell) + abs(fractional) @ cell_error + fraction_error @ cell_error
            native_geometry = evaluation["geometry"]
            position_bound = float(np.max(np.linalg.norm(cart_error, axis=1))) + math.sqrt(3) * native_geometry["position_half_last_place"]
            cell_bound = float(np.max(cell_error)) + native_geometry["cell"]["component_half_last_place"]
            if (comparison["max_displacement_angstrom"] + position_bound > comparison["position_tolerance_angstrom"]
                    or comparison["max_cell_difference_angstrom"] + cell_bound > comparison["cell_tolerance_angstrom"]):
                return "FAIL" if findings else "UNKNOWN", [*findings, {"code": "xml_geometry_precision_insufficient"}]
    else:
        return "UNKNOWN", [*findings, {"code": "xml_geometry_identity_unavailable"}]
    return ("FAIL" if findings else "PASS"), findings


def _band_correspondence(ctx, spectrum):
    """Compare every native band/coordinate/occupation, not only edge values."""
    evaluation, settings = ctx.selected(), ctx.settings()
    points = evaluation.get("band_points", [])
    nspin = settings.get("ISPIN")
    if not points or nspin not in (1, 2):
        return "UNKNOWN", [{"code": "native_band_counterpart_unavailable"}]
    if not spectrum.get("complete"):
        return "UNKNOWN", [{"code": "incomplete_spectrum"}, *spectrum.get("findings", [])]
    dimensions = (spectrum.get("nspin"), spectrum.get("nkpoints"), spectrum.get("nbands"))
    if any(type(value) is not int or value <= 0 for value in dimensions):
        return "FAIL", [{"code": "invalid_spectrum_dimensions"}]
    channels, kpoints = spectrum.get("eigenvalues", []), spectrum.get("kpoints", [])
    if (len(channels) != spectrum["nspin"] or len(kpoints) != spectrum["nkpoints"]
            or any(len(channel) != spectrum["nkpoints"] for channel in channels)
            or any(len(bands) != spectrum["nbands"] for channel in channels for bands in channel)):
        return "FAIL", [{"code": "incomplete_spectrum_arrays"}]
    if nspin != spectrum["nspin"] or len(points) != spectrum["nkpoints"] * nspin:
        return "FAIL", [{"code": "native_band_dimensions_conflict"}]
    if settings.get("NBANDS") != spectrum["nbands"] or settings.get("NKPTS") != spectrum["nkpoints"]:
        return "FAIL", [{"code": "native_band_metadata_conflict"}]
    electron_counts = (settings.get("NELECT"), spectrum.get("nelect"))
    if any(value is None for value in electron_counts):
        return "UNKNOWN", [{"code": "spectrum_electron_count_unavailable"}]
    if (any(type(value) not in (int, float) or not math.isfinite(value) for value in electron_counts)
            or not math.isclose(*electron_counts, abs_tol=1e-6, rel_tol=0)):
        return "FAIL", [{"code": "spectrum_electron_count_conflict"}]
    labels = {(point["spin_index"], point["native_kpoint_label"] - 1) for point in points}
    if labels != {(s, k) for s in range(nspin) for k in range(spectrum["nkpoints"])}:
        return "FAIL", [{"code": "native_band_label_conflict"}]
    for point in points:
        s, k = point["spin_index"], point["native_kpoint_label"] - 1
        if not 0 <= s < nspin or not 0 <= k < spectrum["nkpoints"]:
            return "FAIL", [{"code": "native_band_label_conflict"}]
        other = spectrum["kpoints"][k]
        for axis, (token, coordinate) in enumerate(zip(point["fractional"], other["fractional"])):
            # Native four-decimal coordinates and XML/EIGENVAL printed resolution.
            difference = (token["value"] - coordinate + .5) % 1 - .5 if token["value"] is not None else math.inf
            precision = other["fractional_tokens"][axis]["half_last_place"]
            if abs(difference) > (token["half_last_place"] or 0) + precision + 1e-12:
                return "FAIL", [{"code": "native_kpoint_conflict"}]
        bands = spectrum["eigenvalues"][s][k]
        if len(point["bands"]) != len(bands):
            return "FAIL", [{"code": "native_band_count_conflict"}]
        for native, band in zip(point["bands"], bands):
            if native["native_band_label"] != band["native_band_label"] or not _same_printed(native["energy"], band["energy"]):
                return "FAIL", [{"code": "native_band_energy_conflict", "spin_index": s, "kpoint_index": k}]
            occupation = dict(native["occupation"])
            if occupation["value"] is None:
                return "FAIL", [{"code": "nonfinite_native_occupation"}]
            divisor = 2 if nspin == 1 else 1
            occupation["value"] /= divisor
            occupation["half_last_place"] /= divisor
            if not _same_printed(occupation, band["occupation"]):
                return "FAIL", [{"code": "native_occupation_conflict", "spin_index": s, "kpoint_index": k}]
    return "PASS", []


def _bands(ctx):
    from .analysis_spectra import read_eigenval, analyze_spectrum
    ctx.check()
    if "bands" in ctx._cache:
        return ctx._cache["bands"]
    source = ctx.eigenvalue_source
    if source == "auto":
        source = "xml" if ctx.exists("vasprun.xml") else "eigenval"
    if source == "xml":
        data = _xml(ctx)
        status, findings = _xml_correspondence(ctx, data)
        spectrum = data["spectrum"]
    elif source == "eigenval":
        spectrum = ctx.read("eigenval", "EIGENVAL", read_eigenval)
        status, findings = "PASS", []
        # Separate EIGENVAL has no calculation index; it can only bind the final
        # evaluation, and only via its complete native spectrum counterpart.
        if ctx.selected()["index"] != len(ctx.native()["evaluations"]) - 1:
            status, findings = "UNKNOWN", [{"code": "eigenval_has_no_historical_evaluation_identity"}]
    else:
        raise AnalysisError("invalid_eigenvalue_source", "Use auto, xml or eigenval")
    if spectrum is None:
        raise AnalysisError("spectrum_unavailable", "Selected calculation has no eigenvalue evidence")
    paired, pair_findings = _band_correspondence(ctx, spectrum)
    analysis = analyze_spectrum(spectrum, ctx.settings(), occupation_tolerance=ctx.occupation_tolerance,
                                energy_tolerance_ev=ctx.energy_tolerance_ev, all_points=ctx.all_points)
    result = {"status": _status(status, paired, analysis["status"]), "findings": [*findings, *pair_findings, *analysis["findings"]],
              "analysis": analysis, "source": source}
    ctx._cache["bands"] = result
    return result


def _fermi(ctx):
    records = ctx.selected().get("fermi_records", [])
    native_status, findings = "PASS", []
    reported = None
    if records:
        if any(r["value"] is None for r in records):
            native_status = "FAIL"
            findings.append({"code": "nonfinite_fermi"})
        else:
            reported = min(records, key=lambda r: r["half_last_place"])
            if any(not _same_printed(reported, r) for r in records):
                native_status = "FAIL"
                findings.append({"code": "native_fermi_conflict"})
    source = ctx.fermi_source
    if source == "xml":
        data = _xml(ctx)
        paired, pair_findings = _xml_correspondence(ctx, data)
        native_status = _status(native_status, paired)
        findings += pair_findings
        xml_fermi = (data["selected_calculation"] or {}).get("fermi")
        if xml_fermi is not None and xml_fermi["value"] is None:
            native_status = "FAIL"
            findings.append({"code": "nonfinite_xml_fermi"})
        if reported and xml_fermi and not _same_printed(reported, xml_fermi):
            native_status = "FAIL"
            findings.append({"code": "xml_native_fermi_conflict"})
        reported = xml_fermi
    elif source != "outcar":
        raise AnalysisError("invalid_fermi_source", "Use outcar or xml; never infer Fermi energy from sampled edges")
    convention = ctx.native().get("fermi_convention")
    effective = ctx.settings().get("EFERMI")
    if convention is None:
        convention = effective if effective in {"LEGACY", "MIDGAP"} else "FIXED" if type(effective) in (int, float) else "UNESTABLISHED"
    if convention not in {"LEGACY", "MIDGAP", "FIXED"}:
        native_status = _status(native_status, "UNKNOWN")
        findings.append({"code": "fermi_convention_unestablished"})
    if reported is None:
        native_status = _status(native_status, "UNKNOWN")
        findings.append({"code": "reported_fermi_unavailable"})
    return _publish(ctx, "fermi", reported["value"] if reported else None, native_status,
                    convention="Reported " + convention + " Fermi energy in the native potential gauge",
                    findings=findings, details={"reported": reported, "convention": convention, "source": source,
                    "interpretation": "A semiconductor LEGACY/MIDGAP choice is not a measured chemical potential."})


def get_fermi_level(run: str | Path | AnalysisContext, **options) -> dict:
    """Reported Fermi value; default only OUTCAR and electronic-table evidence."""
    return metric_call(run, "fermi", _fermi, options)


def _edge(ctx, name):
    bands = _bands(ctx)
    entry = bands["analysis"][name]
    value = entry.get("value") if isinstance(entry, dict) else entry
    return _publish(ctx, name, value, bands["status"], convention="Occupation-aware extrema over " + ("all explicitly requested sampled points" if ctx.all_points else "positive-weight integration points"),
                    findings=bands["findings"], details={"source": bands["source"], **bands["analysis"]})


def get_vbm(run: str | Path | AnalysisContext, **options) -> dict:
    """Sampled occupied maximum in eV, with global/per-spin state identities."""
    return metric_call(run, "vbm", lambda ctx: _edge(ctx, "vbm"), options)


def get_cbm(run: str | Path | AnalysisContext, **options) -> dict:
    """Sampled empty minimum in eV; missing empty bands remain unavailable."""
    return metric_call(run, "cbm", lambda ctx: _edge(ctx, "cbm"), options)


def get_band_gap(run: str | Path | AnalysisContext, **options) -> dict:
    """Occupation-aware sampled gap, not a continuous-zone gap guarantee."""
    return metric_call(run, "gap", lambda ctx: _edge(ctx, "gap"), options)


def _native_vacuum(ctx):
    evaluation, settings = ctx.selected(), ctx.settings()
    records = evaluation.get("vacuum_records", [])
    findings = [{"code": "native_vacuum_warning", **w} for w in evaluation.get("vacuum_warnings", [])]
    if not records:
        return {"status": "UNKNOWN", "evidence_status": "UNKNOWN", "sides": {}, "axis": None,
                "findings": [*findings, {"code": "native_vacuum_unavailable"}]}
    evidence_status = "PASS"
    idipol = settings.get("IDIPOL")
    axis = idipol - 1 if type(idipol) is int and idipol in (1, 2, 3) else None
    if settings.get("LVACPOTAV") is not True or axis is None:
        evidence_status = "UNKNOWN"
        findings.append({"code": "native_vacuum_configuration_unestablished"})
    potential = "hartree_plus_ionic"
    if settings.get("LVHAR") is not True or settings.get("LVTOT") is True:
        potential = "xc_containing" if settings.get("LVTOT") is True else "unestablished"
        evidence_status = _status(evidence_status, "UNSUPPORTED")
        findings.append({"code": "native_vacuum_potential_unsupported", "potential_semantics": potential})
    sides = {}
    for side in ("upper", "lower"):
        entries = [r[side] for r in records]
        valid = all(r["value"] is not None for r in entries)
        token = min(entries, key=lambda r: r["half_last_place"] or math.inf)
        status = "PASS" if valid and all(_same_printed(token, r) for r in entries) else "FAIL"
        warnings = [w for w in findings if w["code"] == "native_vacuum_warning"
                    and (side in w["message"].lower() or not any(s in w["message"].lower() for s in ("upper", "lower")))]
        if warnings:
            status = _status(status, "UNKNOWN")
        sides[side] = {"value": token["value"], "units": "eV", "status": _status(status, evidence_status), "reported": token,
                       "findings": warnings}
    return {"status": _status(*(s["status"] for s in sides.values())), "evidence_status": evidence_status,
            "sides": sides, "axis": axis, "potential_semantics": potential, "findings": findings}


def _locpot_xml_precision(ctx, geometry):
    """Resolve rounding only via a higher-precision, validated static endpoint.

    Overlapping error bounds alone do not pass: the entire finer printed
    interval must round into the LOCPOT interval for every ordered component.
    """
    if (ctx.scope()["calculation"] != "static" or not geometry.get("fractional_tokens")
            or not ctx.exists("vasprun.xml")):
        return "UNKNOWN", []
    data = _xml(ctx)
    status, findings = _xml_correspondence(ctx, data)
    if status != "PASS":
        return status, findings
    endpoint = data["selected_calculation"]["geometry"]
    if endpoint["elements"] != geometry["species"]:
        return "FAIL", [{"code": "locpot_xml_species_conflict"}]
    for key in ("cell_tokens", "fractional_tokens"):
        high, low = endpoint[key], geometry[key]
        if len(high) != len(low):
            return "FAIL", [{"code": "locpot_xml_geometry_conflict"}]
        for high_row, low_row in zip(high, low):
            for finer, coarser in zip(high_row, low_row):
                delta = finer["value"] - coarser["value"]
                if key == "fractional_tokens":
                    delta = (delta + .5) % 1 - .5
                if (finer["half_last_place"] >= coarser["half_last_place"]
                        or abs(delta) + finer["half_last_place"] > coarser["half_last_place"] + 1e-14):
                    return "UNKNOWN", [{"code": "locpot_xml_rounding_unresolved"}]
    return "PASS", [{"code": "locpot_geometry_consistent_at_source_precision",
                      "source": "vasprun.xml", "evaluation_index": ctx.selected()["index"]}]


def _vacuum(ctx):
    if ctx.no_vacuum:
        return ctx.result("vacuum", None, status="NOT_APPLICABLE", units="eV", convention="Caller explicitly declared no vacuum applicability")
    native = _native_vacuum(ctx)
    if ctx.vacuum_source == "native":
        data = native
        provenance = "Same native evaluation; LVACPOTAV reference with independently checked potential semantics"
    elif ctx.vacuum_source in {"locpot", "crosscheck"}:
        from .analysis_vacuum import read_locpot
        data = ctx.read("locpot", "LOCPOT", lambda p: read_locpot(p, axis=ctx.axis, settings=ctx.settings(), intervals=ctx.intervals, thresholds=ctx.thresholds))
        data = {**data, "findings": [*data["findings"], *native["findings"]],
                "sides": {k: dict(v) for k, v in data["sides"].items()}}
        comparison = compare_geometry(_native_geometry(ctx), data.get("geometry"))
        data["geometry_comparison"] = comparison
        common = data["evidence_status"]
        require_native = ctx.vacuum_source == "crosscheck" or ctx.locpot_policy == "strict"
        # A standalone LOCPOT has no historical evaluation identity.
        if (ctx.selected()["index"] != len(ctx.native()["evaluations"]) - 1
                or ctx.native()["index"] != len(ctx._cache["native"][0]) - 1):
            common = _status(common, "UNKNOWN")
            data["findings"].append({"code": "locpot_historical_endpoint_unresolved"})
        if comparison["status"] == "mismatch":
            bounds = (data.get("geometry") or {}).get("printed_bounds")
            native_geometry = ctx.selected().get("geometry")
            unresolved = False
            if bounds and native_geometry and comparison["max_displacement_angstrom"] is not None:
                position_bound = bounds["position_angstrom"] + math.sqrt(3) * native_geometry["position_half_last_place"]
                cell_bound = bounds["cell_angstrom"] + native_geometry["cell"]["component_half_last_place"]
                comparison["combined_printed_bounds_angstrom"] = {"position": position_bound, "cell": cell_bound}
                unresolved = (comparison["max_displacement_angstrom"] - position_bound <= comparison["position_tolerance_angstrom"]
                              and comparison["max_cell_difference_angstrom"] - cell_bound <= comparison["cell_tolerance_angstrom"])
            if unresolved:
                geometry_status, geometry_findings = _locpot_xml_precision(ctx, data["geometry"])
                common = _status(common, geometry_status)
                comparison["source_precision_assessment"] = geometry_status
                data["findings"].extend(geometry_findings)
                if geometry_status != "PASS":
                    data["findings"].append({"code": "locpot_geometry_precision_insufficient"})
            else:
                common = "FAIL"
                data["findings"].append({"code": "locpot_geometry_conflict"})
        elif comparison["status"] != "match":
            common = _status(common, "UNKNOWN")
        if native["sides"]:
            common = _status(common, native["evidence_status"])
        if native["axis"] is not None and data["axis"] != native["axis"]:
            common = "FAIL"
            data["findings"].append({"code": "native_locpot_axis_conflict", "native_axis": native["axis"], "locpot_axis": data["axis"]})
        for side, entry in data["sides"].items():
            counterpart = native["sides"].get(side, {})
            token = counterpart.get("reported")
            entry["plateau_status"] = entry["status"]
            entry["generation_binding"] = "unestablished"
            association = common
            native_status = counterpart.get("status", "UNKNOWN")
            corroboration = native_status if native_status != "PASS" else "UNKNOWN"
            if native["sides"]:
                association = _status(association, native_status)
            warnings = [w for w in native["findings"] if w["code"] == "native_vacuum_warning"
                        and (side in w["message"].lower() or not any(s in w["message"].lower() for s in ("upper", "lower")))]
            if warnings:
                association = _status(association, "UNKNOWN")
            if _status(common, native_status, entry["status"]) == "PASS":
                corroboration = "PASS"
                # Use observed variation and actual printed bounds, not a user
                # configurable plateau ceiling that could admit a gauge shift.
                tolerance = (entry["variation_ev"] + entry["half_last_place_ev"]
                             + token["half_last_place"] + ctx.energy_tolerance_ev)
                difference = abs(entry["value"] - token["value"])
                entry["native_correspondence"] = {"difference_ev": difference, "tolerance_ev": tolerance,
                                                   "energy_tolerance_ev": ctx.energy_tolerance_ev,
                                                   "native_reported": token}
                if difference > tolerance:
                    association = corroboration = "FAIL"
                    data["findings"].append({"code": "native_locpot_vacuum_conflict", "side": side,
                                             "difference_ev": difference, "tolerance_ev": tolerance})
            entry["native_corroboration"] = {"available": token is not None, "assessment": corroboration}
            entry["source_consistency"] = ("CONSISTENT_UNAUTHENTICATED" if association == "PASS" else
                                           "CONFLICTING" if association == "FAIL" else "UNRESOLVED")
            entry["scientific_assessment"] = _status(common, entry["plateau_status"])
            entry["qualifications"] = [entry["source_consistency"] + ": source consistency/corroboration does not authenticate LOCPOT generation."]
            paired = _status(association, entry["plateau_status"], corroboration if require_native else "PASS")
            if require_native and corroboration != "PASS":
                data["findings"].append({"code": "locpot_generation_unbound", "side": side,
                                         "message": "This source mode/policy requires valid native corroboration; no generation receipt is verified."})
            entry["status"] = paired
        data["status"] = _status(common, *(entry["status"] for entry in data["sides"].values()))
        data["generation_binding"] = {side: entry["generation_binding"] for side, entry in data["sides"].items()}
        data["locpot_policy"] = ctx.locpot_policy
        provenance = "Explicit LOCPOT; " + ("native corroboration required" if require_native else "source-consistent qualified acceptance") + "; generation unauthenticated"
    else:
        raise AnalysisError("invalid_vacuum_source", "Use native, locpot or crosscheck")
    values = {side: item["value"] for side, item in data["sides"].items()} or None
    result = _publish(ctx, "vacuum", values, data["status"], convention=provenance,
                      findings=data["findings"], details=data)
    # Retain independent side validity even when only one surface is usable.
    gate, _ = ctx.final_gate()
    result["surfaces"] = {side: {"value": item["value"] if gate == "PASS" and item["status"] == "PASS" else None,
                                "assessment": _status(gate, item["status"])}
                          for side, item in data["sides"].items()}
    for side, entry in data["sides"].items():
        if "source_consistency" in entry:
            result["surfaces"][side].update({k: entry[k] for k in ("source_consistency", "native_corroboration", "qualifications")})
            result["surfaces"][side]["scientific_assessment"] = _status(gate, entry["scientific_assessment"])
            result["qualifications"].extend(q for q in entry["qualifications"] if q not in result["qualifications"])
    return result


def get_vacuum_level(run: str | Path | AnalysisContext, **options) -> dict:
    """Separate upper/lower native references; LOCPOT is explicit opt-in."""
    return metric_call(run, "vacuum", _vacuum, options)


def _derived(ctx, name, endpoint):
    if ctx.no_vacuum:
        return ctx.result(name, None, status="NOT_APPLICABLE", units="eV", convention="Caller explicitly declared no vacuum applicability")
    vacuum = _vacuum(ctx)
    level = _fermi(ctx) if endpoint == "fermi" else _edge(ctx, endpoint)
    values, surfaces, derived_findings = {}, {}, []
    for side, reference in vacuum.get("surfaces", {}).items():
        status = _status(reference["assessment"], level["assessment"])
        value = reference["value"] - level["value"] if status == "PASS" and reference["value"] is not None and level["value"] is not None else None
        if value is not None and not math.isfinite(value):
            value, status = None, "FAIL"
            derived_findings.append({"code": "nonfinite_derived_energy", "side": side})
        surfaces[side] = {"value": value, "assessment": status, "units": "eV"}
        surfaces[side].update({k: reference[k] for k in ("source_consistency", "native_corroboration", "qualifications") if k in reference})
        values[side] = value
    status = _status(vacuum["assessment"], level["assessment"], *(s["assessment"] for s in surfaces.values()))
    result = ctx.result(name, values if values and any(v is not None for v in values.values()) else None,
                        status=status, units="eV", convention=f"Each surface: vacuum minus {endpoint}; never average sides",
                        findings=[*vacuum["findings"], *level["findings"], *derived_findings],
                        qualifications=vacuum["qualifications"][2:])
    result["surfaces"] = surfaces
    result["parents"] = {"vacuum": {"identity": vacuum["identity"], "assessment": vacuum["assessment"],
                                   "qualifications": vacuum["qualifications"]},
                         endpoint: {"identity": level["identity"], "assessment": level["assessment"], "convention": level["convention"]}}
    return result


def get_work_function(run: str | Path | AnalysisContext, **options) -> dict:
    """Per-surface vacuum minus reported Fermi energy in eV."""
    return metric_call(run, "work_function", lambda ctx: _derived(ctx, "work_function", "fermi"), options)


def get_ionization_energy(run: str | Path | AnalysisContext, **options) -> dict:
    """Per-surface vacuum minus sampled VBM in eV with compatible parents."""
    return metric_call(run, "ionization_energy", lambda ctx: _derived(ctx, "ionization_energy", "vbm"), options)


def get_electron_affinity(run: str | Path | AnalysisContext, **options) -> dict:
    """Per-surface vacuum minus sampled CBM in eV, retaining negative EA."""
    return metric_call(run, "electron_affinity", lambda ctx: _derived(ctx, "electron_affinity", "cbm"), options)
