"""Read versioned native Jobs execution evidence without modifying Jobs state.

Jobs records foreground completion, not scientific output identities. Schema 4
also requires the original enrolled queue and its retained node lineage. The
finalization specification must separately bind the scientific snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from .result_sources import MAX_JSON_BYTES, SourceSnapshotError, read_json_source


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


def _inspect_execution(
    snapshot_path: Path | str | None,
    receipt_path: Path | str | None,
    *,
    job_id: int,
    attempt_id: str,
    run_directory: Path | str,
    runner_record: dict | None = None,
    native_sources=(),
    expected_target: dict | None = None,
    snapshot_document: dict | None = None,
) -> dict:
    """Check a selected Jobs invocation and its native receipt, read-only.

    ``job_id`` is the numeric Jobs ID; ``attempt_id`` is its operational UUID,
    not the scientific attempt ID carried by an optional execution layout.
    An exported snapshot may be anywhere. The receipt must still have its exact
    recorded ``state_directory/attempts/UUID/payload-exit.json`` location.
    An explicit supported runner record can bind a wrapper input cwd to its
    original payload output cwd. No queue discovery or state mutation occurs.
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
        if role == "jobs_snapshot" and snapshot_document is not None:
            try:
                raw = json.dumps(snapshot_document, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
                if len(raw) > MAX_JSON_BYTES:
                    raise ValueError("snapshot exceeds bounded JSON limit")
                documents[role] = json.loads(raw)
                result["snapshot_identity"] = {"kind": "in-memory-jobs-snapshot",
                                               "sha256": hashlib.sha256(raw).hexdigest()}
                check(role, True, "The supplied snapshot was captured as bounded JSON")
            except (ValueError, TypeError, RecursionError) as exc:
                check(role, False, f"Cannot use in-memory Jobs snapshot: {exc}")
            continue
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
                      and snapshot["schema"] in {2, 3, 4} and isinstance(snapshot.get("jobs"), list)
                      and all(isinstance(item, dict) for item in snapshot["jobs"]))
    check("snapshot_schema", snapshot_shape, "Expected a supported native Jobs schema-2, schema-3 or schema-4 snapshot")
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
        "cancel_requested", "termination_requested", "logs", "worker_protocol",
        "suspension", "node_submission",
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
    result["snapshot_schema"] = snapshot["schema"]
    _inspect_queue(snapshot, job, root, expected_target, result, check)
    suspension = job.get("suspension")
    if suspension is not None:
        supported = (isinstance(suspension, dict) and type(suspension.get("protocol")) is int
                     and suspension["protocol"] == 1
                     and suspension.get("backend") in {"local-command-v1", "vasp-port-v1", "orca-local-v1"})
        check("suspension_schema", supported, "Suspension evidence must use its supported protocol and backend")
        if supported:
            intervals = suspension.get("intervals")
            closed = (isinstance(intervals, list) and all(isinstance(item, dict)
                      and _timestamp(item.get("start")) and _timestamp(item.get("end"))
                      and item["end"] >= item["start"] for item in intervals))
            check("suspension_terminal", suspension.get("phase") == "Run"
                  and suspension.get("desired") == "Run" and closed,
                  "Unverified or unfinished suspension cannot establish clean completion")
    if "termination_requested" in job:
        check("not_terminated", job["termination_requested"] is None or job["termination_requested"] is False,
              "A terminated producer is not successful execution evidence")
    actual_run = _recorded_path(str(Path(run_directory).absolute()))
    if runner_record is None:
        check("working_directory", actual_run is not None and _recorded_path(job.get("cwd")) == actual_run,
              "The recorded working directory must match the inspected run directory")
    else:
        from .result_runner import inspect_runner
        runner = inspect_runner(runner_record, job, receipt, run_directory, native_sources)
        result["sources"].extend(runner["sources"])
        result["runner"] = runner["facts"]
        check("runner_association", runner["valid"], runner["error"] or "Runner association is invalid")
        if runner["valid"]:
            result["binding"].update({key: runner["facts"][key] for key in ("wrapper_cwd", "payload_cwd")})
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


def _inspect_queue(snapshot, job, root, expected_target, result, check):
    """Revalidate one explicit original queue, never discover or create state."""
    schema = snapshot["schema"]
    declared = snapshot.get("queue_identity")
    if schema < 4:
        check("schema_lineage", declared is None and job.get("node_submission") is None
              and (isinstance(snapshot.get("controller", {}), dict)
                   and snapshot.get("controller", {}).get("schema", schema) == schema),
              "Enrolled schema-4 evidence must not be downgraded to a legacy snapshot")
    actual, current = None, None
    if root is not None and (root / "queue.sqlite3").exists():
        from cmw.jobs.lifecycle import read_job
        from cmw.jobs.store import JobsError, Store
        try:
            store = Store(root)
            current, control = read_job(store, job["id"])
            actual = store.queue_identity()
            check("native_queue_schema", control["schema"] == schema,
                  "Snapshot schema must match its original native queue schema")
            keys = ("id", "attempt_id", "engine", "argv", "cwd", "layout", "status", "started_at",
                    "finished_at", "exit_code", "signal", "cancel_requested", "termination_requested",
                    "logs", "node_submission", "worker_protocol")
            same = all(job.get(key) == current.get(key) for key in keys)
            suspension_keys = ("backend", "protocol", "phase", "desired", "revision", "request",
                               "inhibited", "intervals", "capability", "clock", "accounting_uncertain")
            saved_suspension, current_suspension = job.get("suspension") or {}, current.get("suspension") or {}
            same = (same and isinstance(saved_suspension, dict) and isinstance(current_suspension, dict)
                    and all(saved_suspension.get(key) == current_suspension.get(key) for key in suspension_keys))
            check("native_attempt", same, "Saved invocation must match the selected original native attempt")
        except (JobsError, OSError, ValueError, TypeError, KeyError) as exc:
            check("native_queue", False, f"Original queue identity or attempt is unavailable: {exc}")
    elif schema == 4:
        check("native_queue", None, "Enrolled completion requires its original queue to remain available on its owner node")
    if schema == 4:
        check("queue_identity", declared == actual if declared is not None and actual is not None else None,
              "Schema-4 completion needs matching exported original enrollment; re-export older snapshots missing queue_identity")
        if declared is not None:
            result["binding"]["queue_identity"] = declared
    if job.get("node_submission") is not None:
        submission = job["node_submission"]
        valid = (isinstance(submission, dict) and type(submission.get("schema")) is int
                 and submission["schema"] == 1 and actual is not None
                 and all(submission.get(key) == actual[key] for key in ("node_id", "queue_id"))
                 and all(isinstance(submission.get(key), str) and re.fullmatch(pattern, submission[key])
                         for key, pattern in (("request_id", r"[a-f0-9]{32}"), ("digest", r"[a-f0-9]{64}"))))
        check("node_submission", bool(valid), "Recorded node request must match the original enrollment and exact request identity")
        result["binding"]["node_submission"] = submission
    if expected_target is not None:
        required = {"node_id", "queue_id", "job_id", "attempt_id"}
        valid = (isinstance(expected_target, dict) and required <= set(expected_target)
                 and not set(expected_target) - required - {"host_id", "runtime_id"}
                 and type(expected_target.get("job_id")) is int
                 and expected_target["job_id"] == job["id"]
                 and expected_target.get("attempt_id") == job["attempt_id"]
                 and actual is not None
                 and all(expected_target.get(key) == actual[key] for key in ("node_id", "queue_id")))
        if valid and "host_id" in expected_target:
            valid = expected_target["host_id"] == actual["host_id"]
        check("owner_target", bool(valid), "Review target must match the original node, queue and operational attempt")
        if isinstance(expected_target, dict) and "runtime_id" in expected_target:
            submission = job.get("node_submission")
            runtime = submission.get("runtime_id") if isinstance(submission, dict) else None
            check("submission_runtime", expected_target["runtime_id"] == runtime if runtime else None,
                  "Requested runtime identity needs matching recorded submission-runtime evidence")


def inspect_execution(snapshot_path, receipt_path, *, job_id, attempt_id, run_directory,
                      runner_record=None, native_sources=(), expected_target=None):
    """Inspect explicit saved records; retain their complete file source identities."""
    return _inspect_execution(snapshot_path, receipt_path, job_id=job_id, attempt_id=attempt_id,
                              run_directory=run_directory, runner_record=runner_record,
                              native_sources=native_sources, expected_target=expected_target)


def inspect_execution_snapshot(snapshot, receipt_path, *, job_id, attempt_id, run_directory,
                               runner_record=None, native_sources=(), expected_target=None):
    """Inspect a captured native snapshot without writing an export or queue file.

    Its canonical digest is an in-memory observation identity, never a fabricated
    file source. Finalization still requires an explicit saved snapshot/spec.
    """
    return _inspect_execution(None, receipt_path, job_id=job_id, attempt_id=attempt_id,
                              run_directory=run_directory, runner_record=runner_record,
                              native_sources=native_sources, expected_target=expected_target,
                              snapshot_document=snapshot)


__all__ = ["inspect_execution", "inspect_execution_snapshot"]
