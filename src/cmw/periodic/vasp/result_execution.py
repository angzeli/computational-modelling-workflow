"""Read explicitly supplied native Jobs execution evidence without opening Jobs state.

Jobs records foreground completion, not scientific output identities. This adapter
checks an exported schema-2 snapshot against its original payload-exit receipt;
the finalization specification must separately bind the scientific snapshot.
"""

from __future__ import annotations

import math
from pathlib import Path
import re

from .result_sources import SourceSnapshotError, read_json_source


def _timestamp(value: object) -> bool:
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _recorded_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def inspect_execution(
    snapshot_path: Path | str | None,
    receipt_path: Path | str | None,
    *,
    job_id: int,
    attempt_id: str,
    run_directory: Path | str,
) -> dict:
    """Check a selected Jobs invocation and its native receipt, read-only.

    ``job_id`` is the numeric Jobs ID; ``attempt_id`` is its operational UUID,
    not the scientific attempt ID carried by an optional execution layout.
    An exported snapshot may be anywhere. The receipt must still have its exact
    recorded ``state_directory/attempts/UUID/payload-exit.json`` location.
    No queue discovery, process observation or state mutation is performed.
    """
    result = {
        "schema_version": 1,
        "kind": "cmw_jobs_execution_evidence",
        "status": "unknown",
        "operational_success": None,
        "exit_code": None,
        "signal": None,
        "job": None,
        "sources": [],
        "checks": {},
        "findings": [],
        "limitations": [
            "Jobs completion does not establish scientific convergence.",
            "Jobs does not snapshot scientific inputs or outputs; their association requires an explicit finalization binding.",
            "Saved snapshots and receipts are local evidence, not authenticated or tamper-proof records.",
        ],
    }

    def check(name: str, value: bool | None, message: str) -> None:
        result["checks"][name] = value
        if value is not True:
            result["findings"].append({
                "code": f"execution.{name}",
                "severity": "error" if value is False else "unknown",
                "scope": "execution",
                "message": message,
            })

    def finish() -> dict:
        values = tuple(result["checks"].values())
        result["status"] = "invalid" if False in values else "unknown" if None in values else "eligible"
        result["operational_success"] = (
            True if result["status"] == "eligible" else False if result["status"] == "invalid" else None
        )
        return result

    check("selection", type(job_id) is int and job_id > 0
          and isinstance(attempt_id, str) and re.fullmatch(r"[0-9a-f]{32}", attempt_id) is not None,
          "An exact positive Jobs ID and lowercase operational attempt UUID are required")
    if result["checks"]["selection"] is False:
        return finish()

    documents = {}
    for role, path in (("jobs_snapshot", snapshot_path), ("jobs_receipt", receipt_path)):
        if path is None:
            check(role, None, f"Explicit {role} is required for operational completion evidence")
            continue
        try:
            value, source = read_json_source(path, role)
        except SourceSnapshotError as exc:
            if exc.record:
                result["sources"].append(exc.record)
            check(role, None, f"Cannot use {role}: {exc}")
            continue
        result["sources"].append(source)
        check(role, source.get("stable") is True and source.get("coverage") == "complete",
              f"The {role} source must be stable and completely observed")
        documents[role] = value
    if len(documents) != 2:
        return finish()

    snapshot = documents["jobs_snapshot"]
    receipt = documents["jobs_receipt"]
    snapshot_shape = (isinstance(snapshot, dict) and type(snapshot.get("schema")) is int
                      and snapshot["schema"] == 2 and isinstance(snapshot.get("jobs"), list)
                      and all(isinstance(item, dict) for item in snapshot["jobs"]))
    check("snapshot_schema", snapshot_shape, "Expected an exported native Jobs schema-2 snapshot")
    receipt_shape = (isinstance(receipt, dict) and set(receipt) == {"exit_code", "recorded_at"}
                     and type(receipt.get("exit_code")) is int and 0 <= receipt["exit_code"] <= 255
                     and _timestamp(receipt.get("recorded_at")))
    check("receipt_schema", receipt_shape, "Expected native payload-exit.json with integer exit_code and finite recorded_at")
    if not snapshot_shape or not receipt_shape:
        return finish()

    selected = [item for item in snapshot["jobs"] if item.get("id") == job_id
                or item.get("attempt_id") == attempt_id]
    exact = (len(selected) == 1 and type(selected[0].get("id")) is int
             and selected[0]["id"] == job_id and selected[0].get("attempt_id") == attempt_id)
    check("job_identity", exact, "Jobs ID and attempt UUID must identify exactly one matching snapshot job")
    if not exact:
        return finish()
    job = selected[0]
    # Preserve only execution fields used here, not arbitrary saved environment secrets.
    result["job"] = {name: job.get(name) for name in (
        "id", "display_id", "attempt_id", "engine", "argv", "cwd", "layout",
        "status", "started_at", "finished_at", "exit_code", "signal",
        "cancel_requested", "logs",
    )}
    result["exit_code"] = job.get("exit_code")
    result["signal"] = job.get("signal")
    root = _recorded_path(snapshot.get("state_directory"))
    actual_receipt = _recorded_path(str(Path(receipt_path).absolute()))
    expected_receipt = None if root is None else root / "attempts" / attempt_id / "payload-exit.json"
    check("receipt_attempt_location", root is not None and actual_receipt == expected_receipt,
          "Receipt must occupy the selected Jobs attempt's recorded native location")
    result["binding"] = {
        "state_directory": None if root is None else str(root),
        "expected_receipt": None if expected_receipt is None else str(expected_receipt),
        "operational_attempt_id": attempt_id,
        "scientific_layout": job.get("layout"),
        "scientific_source_binding": "not_provided_by_jobs",
    }
    actual_run = _recorded_path(str(Path(run_directory).absolute()))
    check("working_directory", actual_run is not None and _recorded_path(job.get("cwd")) == actual_run,
          "The recorded working directory must match the inspected run directory")
    argv = job.get("argv")
    check("command", isinstance(argv, list) and bool(argv)
          and all(isinstance(arg, str) and "\0" not in arg for arg in argv) and bool(argv[0]),
          "The snapshot must retain the explicit nonempty foreground command")
    logs = job.get("logs")
    check("attempt_logs", root is not None and isinstance(logs, dict)
          and all(_recorded_path(logs.get(stream)) == root / "attempts" / attempt_id / f"{stream}.log"
                  for stream in ("stdout", "stderr")),
          "The snapshot's log paths must belong to the selected Jobs attempt")

    status = job.get("status")
    terminal = True if status == "Done" else False if status in {"Fail", "Cancelled"} else None
    check("terminal_done", terminal, "The selected Jobs invocation must record terminal Done")
    exit_code = job.get("exit_code")
    check("exit_code_type", type(exit_code) is int if exit_code is not None else None,
          "The snapshot must record an integer process exit code")
    if type(exit_code) is int:
        check("exit_agreement", exit_code == receipt["exit_code"], "Snapshot and receipt exit codes conflict")
        check("exit_zero", exit_code == 0, "The foreground command did not exit successfully")
    else:
        check("exit_agreement", None, "Snapshot exit evidence is absent or malformed")
    check("no_signal", job.get("signal") is None if "signal" in job else None,
          "Signal absence must be explicitly recorded; a signalled job is ineligible")
    check("not_cancelled", job.get("cancel_requested") is False if "cancel_requested" in job else None,
          "The selected invocation must explicitly record no cancellation request")
    times = [job.get("started_at"), receipt["recorded_at"], job.get("finished_at"), snapshot.get("observed_at")]
    valid_times = all(_timestamp(value) for value in times)
    check("timestamps", valid_times if all(value is not None for value in times) else None,
          "Start, receipt, finish and snapshot observations require finite numeric timestamps")
    if valid_times:
        check("time_order", times == sorted(times), "Receipt must lie within the selected attempt and precede its saved snapshot")
    return finish()


__all__ = ["inspect_execution"]
