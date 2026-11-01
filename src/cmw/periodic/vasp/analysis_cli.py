"""Thin, read-only presentation for independent VASP analysis metrics."""
from __future__ import annotations

import argparse
import json


def _analyze(args: argparse.Namespace) -> int:
    from .analysis_context import AnalysisContext
    from . import electronic_levels as levels
    from . import scf_analysis as scf

    getters = {"fermi": levels.get_fermi_level, "vbm": levels.get_vbm,
               "cbm": levels.get_cbm, "gap": levels.get_band_gap,
               "vacuum": levels.get_vacuum_level, "work-function": levels.get_work_function,
               "ionization-energy": levels.get_ionization_energy, "electron-affinity": levels.get_electron_affinity}
    try:
        options = {k: getattr(args, k) for k in ("segment", "evaluation", "stdout", "outcar", "oszicar", "diagnostic")}
        if args.analysis_kind == "electronic":
            requested = args.only.split(",")
            if not requested or len(set(requested)) != len(requested) or any(k not in getters for k in requested):
                raise ValueError("--only requires unique comma-separated names: " + ",".join(getters))
            options.update({k: getattr(args, k) for k in ("xml", "eigenval", "locpot", "eigenvalue_source", "fermi_source", "vacuum_source",
                           "locpot_policy", "axis", "occupation_tolerance", "energy_tolerance_ev", "all_points", "no_vacuum")})
            options["intervals"] = {side: getattr(args, side + "_interval") for side in ("upper", "lower") if getattr(args, side + "_interval") is not None}
            options["thresholds"] = {key: getattr(args, key) for key in ("min_width_angstrom", "max_variation_ev", "max_abs_slope_ev_per_angstrom",
                                      "atomic_exclusion_angstrom", "discontinuity_exclusion_angstrom", "jump_threshold_ev") if getattr(args, key) is not None}
        else:
            getters = {"history": scf.get_scf_history, "convergence": scf.get_scf_convergence,
                       "timing": scf.get_scf_timing, "summary": scf.get_scf_summary}
            requested = ["summary"] if args.only is None else args.only.split(",")
            if len(set(requested)) != len(requested) or any(k not in getters for k in requested):
                raise ValueError("SCF --only supports history,convergence,timing,summary")
        ctx = AnalysisContext(args.directory, **options)
        results = {name: getters[name](ctx) for name in requested}
        statuses = {r["assessment"] for r in results.values()}
        exit_code = 1 if "FAIL" in statuses else 2 if statuses & {"UNKNOWN", "UNSUPPORTED"} else 0
        result = {"schema_version": 1, "analysis": args.analysis_kind, "metrics": results, "exit_code": exit_code}
    except (ValueError, TypeError, OSError) as error:
        exit_code = 2
        result = {"schema_version": 1, "assessment": "UNKNOWN", "value": None,
                  "findings": [{"code": "invalid_request", "message": str(error)}], "exit_code": exit_code}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    elif "metrics" in result:
        for name, metric in result["metrics"].items():
            print(f"{name}: {metric['assessment']}; value={metric['value']}")
            for finding in metric["findings"]:
                print("  " + finding["code"] + (": " + finding["message"] if "message" in finding else ""))
    else:
        print(result["findings"][0]["message"])
    return exit_code


def register(commands: argparse._SubParsersAction) -> None:
    for kind in ("scf", "electronic"):
        parser = commands.add_parser("analyze-" + kind, help="read-only modular VASP " + kind + " analysis; no execution or artifact acceptance")
        parser.set_defaults(handler=_analyze, analysis_kind=kind)
        parser.add_argument("directory")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--segment", type=int, help="zero-based native execution segment")
        parser.add_argument("--evaluation", type=int, help="zero-based electronic evaluation; defaults to latest, including partial")
        parser.add_argument("--outcar")
        parser.add_argument("--oszicar")
        parser.add_argument("--stdout", help="explicit electronic stdout, also cross-checked with present OSZICAR")
        parser.add_argument("--diagnostic", action="store_true", help="expose provisional observations without promoting derived quantities")
        parser.add_argument("--only", required=kind == "electronic", help="comma-separated requested metric names")
        if kind == "scf":
            continue
        for source in ("xml", "eigenval", "locpot"):
            parser.add_argument("--" + source, help="explicit source path; never silently substituted")
        parser.add_argument("--eigenvalue-source", choices=("auto", "xml", "eigenval"), default="auto")
        parser.add_argument("--fermi-source", choices=("outcar", "xml"), default="outcar")
        parser.add_argument("--vacuum-source", choices=("native", "locpot", "crosscheck"), default="native")
        parser.add_argument("--locpot-policy", choices=("standard", "strict"), default="standard",
                            help="standard accepts source-consistent, unauthenticated LOCPOT; strict also requires native corroboration")
        parser.add_argument("--axis", type=int, choices=(0, 1, 2), help="zero-based lattice axis; otherwise native IDIPOL")
        for side in ("upper", "lower"):
            parser.add_argument("--" + side + "-interval", nargs=2, type=float, metavar=("START_A", "END_A"))
        parser.add_argument("--no-vacuum", action="store_true", help="explicit no-vacuum applicability, never inferred from name/geometry")
        parser.add_argument("--all-points", action="store_true", help="sample-qualified extrema including zero-weight points")
        parser.add_argument("--occupation-tolerance", type=float, default=1e-3)
        parser.add_argument("--energy-tolerance-ev", type=float, default=1e-5)
        for threshold in ("min-width-angstrom", "max-variation-ev", "max-abs-slope-ev-per-angstrom", "atomic-exclusion-angstrom",
                          "discontinuity-exclusion-angstrom", "jump-threshold-ev"):
            parser.add_argument("--" + threshold, type=float)
