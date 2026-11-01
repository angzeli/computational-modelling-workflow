"""Independent, complete-history SCF analysis; never launches or accepts a job."""
from __future__ import annotations

import math
from pathlib import Path

from .analysis_context import AnalysisContext, metric_call

__all__ = ["AnalysisContext", "get_scf_history", "get_scf_convergence", "get_scf_timing", "get_scf_summary"]


def _history(ctx: AnalysisContext) -> dict:
    native, tables = ctx.native(), ctx.tables()
    evaluations = native["evaluations"] if ctx.evaluation is None else [ctx.selected()]
    value = [{**e, "electronic_table": tables[e["index"]]} for e in evaluations]
    conflict = any(t["correspondence"] == "conflict" for t in tables)
    scope = ctx.scope()
    return ctx.result("scf_history", value, status="FAIL" if conflict or native["conflicts"] else "PASS" if scope["supported"] else "UNSUPPORTED",
                      convention="Chronological native electronic evaluations, not accepted ionic steps",
                      qualifications=["History availability does not establish convergence or termination."],
                      findings=native["conflicts"] + native.get("analysis_findings", []))


def get_scf_history(run: str | Path | AnalysisContext, **options) -> dict:
    """Full signed rows, native labels and all selected-segment evaluations."""
    return metric_call(run, "scf_history", _history, options)


def _convergence(ctx: AnalysisContext) -> dict:
    value = ctx.convergence()
    scope = ctx.scope()
    status = value["assessment"] if scope["supported"] else "UNSUPPORTED"
    return ctx.result("scf_convergence", value, status=status,
                      convention="Both printed-resolution-aware EDIFF criteria and native stop evidence",
                      findings=[{"code": r} for r in scope["reasons"]])


def get_scf_convergence(run: str | Path | AnalysisContext, **options) -> dict:
    """Latest chronological evaluation by default, even when incomplete."""
    return metric_call(run, "scf_convergence", _convergence, options)


def _timing(ctx: AnalysisContext) -> dict:
    native = ctx.native()
    evaluations = native["evaluations"] if ctx.evaluation is None else [ctx.selected()]
    loops, outer, issues = [], [], []
    for evaluation in evaluations:
        for row in evaluation["iterations"]:
            if row.get("loop_timing"):
                loops.append({"evaluation_index": evaluation["index"], "electronic_label": row["label"], **row["loop_timing"]})
            if row.get("timing_issue"):
                issues.append({"code": "invalid_loop_timing", "message": row["timing_issue"]})
        if evaluation.get("loop_timing"):
            outer.append({"evaluation_index": evaluation["index"], **evaluation["loop_timing"]})
        if evaluation.get("timing_issue"):
            issues.append({"code": "invalid_loop_timing", "message": evaluation["timing_issue"]})
    def total(records):
        totals = {}
        for kind in ("cpu", "wall"):
            value = sum(r[kind]["value"] for r in records) if records else None
            if value is not None and not math.isfinite(value):
                value = None
                issues.append({"code": "nonfinite_timing_sum", "kind": kind})
            totals[kind + "_seconds"] = value
        return totals
    value = {"electronic_loops": loops, "electronic_loop_count": len(loops), "electronic_loop_sum": total(loops),
             "outer_loops": outer, "outer_loop_sum": total(outer), "whole_run": native["termination"]["timing"],
             "coverage": {"electronic_complete": len(loops) == sum(len(e["iterations"]) for e in evaluations),
                          "outer_complete": len(outer) == len(evaluations)}}
    return ctx.result("scf_timing", value, status="FAIL" if issues else "PASS" if loops and all(value["coverage"].values()) else "UNKNOWN",
                      units="s", convention="Native LOOP, LOOP+ and whole-run totals are separate, never added together", findings=issues)


def get_scf_timing(run: str | Path | AnalysisContext, **options) -> dict:
    """Separate native LOOP, LOOP+ and whole-run CPU/wall seconds."""
    return metric_call(run, "scf_timing", _timing, options)


def _summary(ctx: AnalysisContext) -> dict:
    native, convergence = ctx.native(), ctx.convergence()
    status, findings = ctx.final_gate()
    return ctx.result("scf_summary", {"evaluation_count": len(native["evaluations"]),
                      "selected_evaluation": ctx.selected()["index"], "convergence": convergence,
                      "timing": _timing(ctx), "scope": ctx.scope()}, status=status,
                      convention="SCF evidence summary, not artifact acceptance", findings=findings)


def get_scf_summary(run: str | Path | AnalysisContext, **options) -> dict:
    """Summarize selected SCF evidence, without artifact or Jobs acceptance."""
    return metric_call(run, "scf_summary", _summary, options)
