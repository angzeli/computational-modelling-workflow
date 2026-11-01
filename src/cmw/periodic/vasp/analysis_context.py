"""Lazy, request-local native analysis evidence, independent of Jobs/finalization."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Callable

from cmw.core.provenance import stable_hash
from .result_sources import SourceSnapshotError, _regular_path, _stat
from .results import _scope, below_printed, parse_oszicar, parse_outcar


class AnalysisError(ValueError):
    def __init__(self, code: str, message: str, status: str = "UNKNOWN"):
        self.code, self.status = code, status
        super().__init__(message)


@dataclass
class AnalysisContext:
    """One immutable-source request; construction/import never opens a file.

    Do not edit options after first use. Start a new context to observe new data.
    Paths are explicit overrides, not inferred aliases for similarly named runs.
    """

    directory: str | Path
    segment: int | None = None
    evaluation: int | None = None
    outcar: str | Path | None = None
    oszicar: str | Path | None = None
    stdout: str | Path | None = None
    xml: str | Path | None = None
    eigenval: str | Path | None = None
    locpot: str | Path | None = None
    eigenvalue_source: str = "auto"
    fermi_source: str = "outcar"
    diagnostic: bool = False
    all_points: bool = False
    occupation_tolerance: float = 1e-3
    energy_tolerance_ev: float = 1e-5
    vacuum_source: str = "native"
    locpot_policy: str = "standard"
    axis: int | None = None
    intervals: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)
    no_vacuum: bool = False
    _cache: dict = field(default_factory=dict, init=False, repr=False)
    _sources: dict = field(default_factory=dict, init=False, repr=False)
    _watched: dict = field(default_factory=dict, init=False, repr=False)
    _invalid: str | None = field(default=None, init=False, repr=False)
    _options: str | None = field(default=None, init=False, repr=False)

    def path(self, role: str) -> Path:
        names = {"OUTCAR": "outcar", "OSZICAR": "oszicar", "stdout": "stdout",
                 "vasprun.xml": "xml", "EIGENVAL": "eigenval", "LOCPOT": "locpot"}
        override = getattr(self, names[role])
        return Path(override) if override is not None else Path(self.directory) / role

    def _option_key(self) -> str:
        return repr({k: v for k, v in vars(self).items() if not k.startswith("_")})

    def check(self) -> None:
        if self._options is None:
            if self.locpot_policy not in ("standard", "strict"):
                raise AnalysisError("invalid_locpot_policy", "Use standard or strict")
            for key in ("occupation_tolerance", "energy_tolerance_ev"):
                value = getattr(self, key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise AnalysisError("invalid_option", key + " must be finite and positive")
            if self.occupation_tolerance >= .5:
                raise AnalysisError("invalid_option", "occupation_tolerance must be below 0.5")
            self._options = self._option_key()
        elif self._options != self._option_key():
            self._invalid = "Analysis options changed during this request"
        for path, before in self._watched.items():
            try:
                after = _stat(_regular_path(Path(path)))
            except FileNotFoundError:
                after = None
            except (OSError, ValueError):
                after = "unsafe"
            if after != before:
                self._invalid = "Source changed during this request: " + path
        if self._invalid:
            raise AnalysisError("request_invalidated", self._invalid)

    def exists(self, role: str) -> bool:
        self.check()
        path = self.path(role).absolute()
        try:
            state = _stat(_regular_path(path))
        except FileNotFoundError:
            state = None
        except (OSError, ValueError) as error:
            raise AnalysisError("unsafe_source", str(error), "FAIL") from error
        self._watched.setdefault(str(path), state)
        return state is not None

    def read(self, key: str, role: str, reader: Callable) -> object:
        self.check()
        if key not in self._cache:
            self.exists(role)
            try:
                data = reader(self.path(role))
            except SourceSnapshotError as error:
                self._sources[role] = error.record
                raise AnalysisError(error.code, str(error), "UNKNOWN" if error.code == "source.missing" else "FAIL") from error
            except (ValueError, OSError) as error:
                raise AnalysisError("invalid_" + role, str(error), "FAIL") from error
            source = data[1] if isinstance(data, tuple) else data["source"]
            self._sources[role] = source
            if source.get("changes"):
                self._invalid = "Source changed while reading: " + role
            self._cache[key] = data
        self.check()
        return self._cache[key]

    def native(self) -> dict:
        segments, _ = self.read("native", "OUTCAR", lambda p: parse_outcar(p, analysis=True))
        if not segments:
            raise AnalysisError("no_segment", "OUTCAR has no execution segment")
        if self.segment is None and len(segments) != 1:
            raise AnalysisError("select_segment", "Multiple execution segments require a zero-based segment selection")
        index = self.segment if self.segment is not None else 0
        if type(index) is not int or not 0 <= index < len(segments):
            raise AnalysisError("invalid_segment", "Selected zero-based segment does not exist")
        return segments[index]

    def selected(self) -> dict:
        evaluations = self.native()["evaluations"]
        index = self.evaluation if self.evaluation is not None else len(evaluations) - 1
        if type(index) is not int or not 0 <= index < len(evaluations):
            raise AnalysisError("invalid_evaluation", "Selected zero-based electronic evaluation does not exist")
        return evaluations[index]

    def settings(self) -> dict:
        segment = self.native()
        return {k: v["value"] for k, v in {**segment["settings"], **segment.get("analysis_settings", {})}.items()}

    def scope(self) -> dict:
        segment, values = self.native(), self.settings()
        scope = _scope(segment)
        if values.get("LHFCALC") is True:
            hse = (scope["calculation"] == "static" and values.get("IALGO") == 38
                   and values.get("GGA") == "PE" and values.get("AEXX") == .25
                   and values.get("HFSCREEN") == .2)
            if hse:
                scope["reasons"] = [r for r in scope["reasons"] if r != "unsupported_mode:LHFCALC"]
                if any(i["algorithm"] != "DAV" for e in segment["evaluations"] for i in e["iterations"]):
                    scope["reasons"].append("outside_bounded_HSE06_static_DAV")
            else:
                scope["reasons"].append("outside_bounded_HSE06_static_DAV")
        if any(i["algorithm"] not in {"DAV", "RMM"} for e in segment["evaluations"] for i in e["iterations"]):
            scope["reasons"].append("unsupported_native_algorithm")
        scope["supported"] = not scope["reasons"]
        return scope

    def tables(self) -> list[dict]:
        from .analysis_evidence import correspondence_scan, duplicate_scan

        self.check()
        if "tables" in self._cache:
            return self._cache["tables"]
        segment = self.native()
        streams = {}
        for role in ("OSZICAR", "stdout"):
            if role == "stdout" and self.stdout is None:
                continue
            if self.exists(role):
                groups, _ = self.read(role, role, lambda p, role=role: parse_oszicar(p, role, complete_lines_only=True))
                if len(groups) > len(self._cache["native"][0]):
                    raise AnalysisError("surplus_table_segments", "Electronic source has unmatched execution segments", "FAIL")
                if len(self._cache["native"][0]) > 1 and groups and len(groups) != len(self._cache["native"][0]):
                    raise AnalysisError("unresolved_table_segments", "Electronic table segments cannot be aligned with OUTCAR")
                streams[role] = groups[segment["index"]] if len(groups) > segment["index"] else []
                if len(streams[role]) > len(segment["evaluations"]):
                    raise AnalysisError("surplus_table_evaluations", "Electronic source is ahead of native evaluation boundaries")
        # An explicit stdout can supply a complete stream while OSZICAR buffers.
        primary = "stdout" if self.stdout is not None else "OSZICAR"
        result = []
        for evaluation in segment["evaluations"]:
            native = {"rows": [{"label": r["label"], "algorithm": r["algorithm"], "energy": r["electronic_free_energy"]} for r in evaluation["iterations"]],
                      "native": evaluation["native_ionic_label"], "complete": evaluation["complete"], "energies": evaluation["energies"]}
            items = {role: rows[evaluation["index"]] if len(rows) > evaluation["index"] else None for role, rows in streams.items()}
            comparisons = {role: correspondence_scan(native, item) for role, item in items.items()}
            relation, matched, reason = comparisons.get(primary, ("pending", [], "Electronic table unavailable"))
            duplicate = None
            if items.get("OSZICAR") and items.get("stdout"):
                duplicate = duplicate_scan(items["OSZICAR"], items["stdout"])
            conflicts = [message for status, _, message in comparisons.values() if status == "conflict"]
            if duplicate and duplicate[0] == "CONFLICT":
                conflicts.append(duplicate[-1])
            if not conflicts and (any(c[0] == "unsupported" for c in comparisons.values()) or duplicate and duplicate[0] == "UNSUPPORTED"):
                relation, reason = "unsupported", "A complete sibling electronic record cannot be validated"
            if not conflicts and any(self._sources[role].get("partial_trailing_line") for role in streams):
                relation, reason = "pending", "An electronic source ends within a record"
            result.append({"correspondence": "conflict" if conflicts else relation, "matched_rows": matched,
                           "source_rows": {role: item["rows"] if item else [] for role, item in items.items()},
                           "reason": "; ".join(conflicts) if conflicts else reason,
                           "source_comparisons": {role: {"status": c[0], "reason": c[2]} for role, c in comparisons.items()},
                           "duplicate": None if duplicate is None else {"status": duplicate[0], "fields": list(duplicate[2]), "reason": duplicate[3]}})
        self._cache["tables"] = result
        return result

    def convergence(self) -> dict:
        evaluation, settings = self.selected(), self.settings()
        table = self.tables()[evaluation["index"]]
        rows, native = evaluation["iterations"], evaluation["native_convergence"]
        final = table["matched_rows"][-1] if rows and len(table["matched_rows"]) == len(rows) else None
        ediff = settings.get("EDIFF")
        ediff = ediff if type(ediff) in (int, float) else None
        checks = {name: below_printed((final or {}).get(name), ediff) for name in ("dE", "d_eps")}
        numeric = ("NOT_APPLICABLE" if ediff == 0 else "FAIL" if "FAIL" in checks.values()
                   else "PASS" if all(v == "PASS" for v in checks.values()) else "UNKNOWN")
        minimum = settings.get("NELMIN")
        min_met = len(rows) >= minimum if type(minimum) is int else None
        limit = settings.get("NELM")
        budget = len(rows) >= limit if type(limit) is int else None
        status = "PASS" if numeric == "PASS" and native is True and min_met is True else "UNKNOWN"
        if native is False or numeric == "FAIL" or (min_met is False and (evaluation["complete"] or native is True)) or table["correspondence"] == "conflict":
            status = "FAIL"
        if ediff == 0:
            status = "NOT_APPLICABLE"
        if table["correspondence"] != "matched" and status == "PASS":
            status = "UNKNOWN"
        if table["correspondence"] == "unsupported":
            status = "UNSUPPORTED"
        if self.native()["conflicts"] or any(f["code"] != "partial_trailing_line" for f in self.native().get("analysis_findings", [])):
            status = "FAIL"
        elif self.native().get("analysis_findings") and status == "PASS":
            status = "UNKNOWN"
        return {"assessment": status, "numeric_convergence": numeric, "native_convergence": native,
                "numeric_checks": checks, "effective_ediff_ev": ediff, "fixed_count": ediff == 0,
                "effective_nelm": limit, "minimum_iterations": minimum, "minimum_iterations_met": min_met,
                "nelm_reached": budget, "nelm_exhausted_without_convergence": budget is True and native is not True,
                "electronic_iteration_count": len(rows), "evaluation_complete": evaluation["complete"],
                "final_electronic_row": final, "correspondence": table,
                "termination": self.native()["termination"]}

    def final_gate(self) -> tuple[str, list[dict]]:
        segment, evaluation = self.native(), self.selected()
        scope = self.scope()
        conflicts = segment["conflicts"] + segment.get("analysis_findings", [])
        if conflicts:
            status = "UNKNOWN" if all(x["code"] == "partial_trailing_line" for x in conflicts) else "FAIL"
            return status, conflicts
        if not scope["supported"]:
            return "UNSUPPORTED", [{"code": reason} for reason in scope["reasons"]]
        convergence = self.convergence()
        term = segment["termination"]
        if term["fatal"] or term["requested_stop"]:
            return "FAIL", [{"code": "native_fatal_or_stop"}]
        if convergence["assessment"] != "PASS":
            status = convergence["assessment"] if convergence["assessment"] != "NOT_APPLICABLE" else "UNKNOWN"
            return status, [{"code": "electronic_convergence_unestablished", "convergence": convergence}]
        if not evaluation["complete"] or not term["normal_footer"]:
            return "UNKNOWN", [{"code": "incomplete_native_evaluation_or_execution"}]
        return "PASS", []

    def result(self, metric: str, value, *, status: str = "PASS", units: str | None = None,
               convention: str | None = None, findings=(), qualifications=(), observed=None) -> dict:
        self.check()
        native = self._cache.get("native")
        segment = None
        if native and (self.segment is not None or len(native[0]) == 1):
            index = self.segment or 0
            if 0 <= index < len(native[0]):
                segment = native[0][index]
        evaluation = self.evaluation if self.evaluation is not None else (len(segment["evaluations"]) - 1 if segment else None)
        identity = {"segment_index": segment["index"] if segment else None, "segment_id": segment["segment_id"] if segment else None,
                    "evaluation_index": evaluation}
        sources = list(self._sources.values())
        return deepcopy({"schema_version": 1, "metric": metric, "value": value, "units": units,
                         "assessment": status, "available": value is not None, "provisional": status != "PASS" and (value is not None or observed is not None),
                         "observed": observed, "convention": convention, "identity": identity,
                         "source_references": sources, "evidence_id": stable_hash({"identity": identity, "sources": [{"role": s["role"], "sha256": s["sha256"]} for s in sources]}),
                         "qualifications": ["Native analysis only; managed execution/input binding unestablished.", "Sequential source snapshots are not atomic.", *qualifications],
                         "findings": list(findings)})


def context(value: str | Path | AnalysisContext, **options) -> AnalysisContext:
    if isinstance(value, AnalysisContext):
        if options:
            raise ValueError("Pass analysis options on the shared context, not individual calls")
        return value
    return AnalysisContext(value, **options)


def metric_call(value, metric: str, operation: Callable, options: dict) -> dict:
    """Convert unavailable/invalid evidence to a structured per-metric outcome."""
    ctx = context(value, **options)
    try:
        ctx.check()
        return operation(ctx)
    except AnalysisError as error:
        # A stale context must remain unusable, including for cached results.
        return {"schema_version": 1, "metric": metric, "value": None, "units": None,
                "assessment": error.status, "available": False, "provisional": False, "observed": None,
                "convention": None, "identity": None, "source_references": deepcopy(list(ctx._sources.values())),
                "qualifications": [], "findings": [{"code": error.code, "message": str(error)}]}
