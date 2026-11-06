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
import sqlite3

from .result_differences import field_differences
from .result_sources import MAX_JSON_BYTES, SourceSnapshotError, read_json_source


_JOB_FIELDS = ("id", "attempt_id", "engine", "argv", "cwd", "layout", "status", "started_at",
               "finished_at", "exit_code", "signal", "cancel_requested", "termination_requested",
               "logs", "node_submission", "worker_protocol")
_SUSPENSION_FIELDS = ("backend", "protocol", "phase", "desired", "revision", "request",
                      "inhibited", "intervals", "capability", "clock", "accounting_uncertain")
_LOCATION_EXAMPLE = {"state_root": "", "root_identity": [0, 0], "database_identity": [0, 0]}
_ENROLLMENT_EXAMPLE = {**_LOCATION_EXAMPLE, "schema": 1, "node_id": "", "queue_id": "", "host_id": ""}
_OPTIONAL_EXAMPLES = {
    "signal": 0, "exit_code": 0, "worker_protocol": 0, "finished_at": 0.0, "started_at": 0.0,
    "termination_requested": False, "layout": {"path": "", "target_id": "", "attempt_id": ""},
    "clock": {"kind": "", "boot": 0.0},
    "node_submission": {"schema": 1, "node_id": "", "queue_id": "", "request_id": "", "digest": "", "runtime_id": ""},
}
_UNEXPECTED = object()
_REDACTED = {"present": True, "redacted": True}


def _diagnostic_value(value, example):
    """An unexpected field exposes its presence; an unexpected type exposes its name."""
    if example is _UNEXPECTED:
        return _REDACTED
    if value is None:
        return None
    if isinstance(value, dict) and isinstance(example, dict):
        return {key: _diagnostic_value(item, example.get(key, _UNEXPECTED)) for key, item in value.items()}
    if isinstance(value, list) and isinstance(example, list):
        item_example = example[0] if example else _UNEXPECTED
        return [_diagnostic_value(item, example[i] if i < len(example) else item_example) for i, item in enumerate(value)]
    if type(value) is type(example) or (type(value) in (int, float) and type(example) is float):
        return value
    return {"type": type(value).__name__}


def _native_differences(field, stored, observed, example):
    # Redaction only formats the result. Two different private values can have
    # the same safe display and must still contradict the original record.
    if not field_differences(field, stored, observed):
        return []
    before, after = _diagnostic_value(stored, example), _diagnostic_value(observed, example)
    if type(stored) is not type(observed):
        if field == "queue_identity" and stored is None and isinstance(observed, dict):
            return [{"field": field, "stored": None, "observed": {"type": "dict"}},
                    *field_differences(field, {}, after)]
        return [{"field": field, "stored": before, "observed": after}]
    return field_differences(field, before, after) or [{"field": field, "stored": before, "observed": after}]


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
    require_live_queue: bool = False,
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
        live_status = result.get("live_queue", {}).get("status")
        if False in values or live_status == "CONTRADICTED":
            result["status"] = "invalid"
        elif live_status == "UNAVAILABLE":
            result["status"] = "unverifiable"
        else:
            result["status"] = "unknown" if None in values else "eligible"
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
    _inspect_queue(snapshot, job, root, expected_target, result, check, require_live_queue)
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


def _selected_fields(record, fields):
    # Native legacy records can omit optional fields equivalent to explicit null.
    return {key: record.get(key) for key in fields}


def _native_field_differences(prefix, saved, current, fields):
    differences = []
    for key in fields:
        stored, observed = saved.get(key), current.get(key)
        example = stored if stored is not None else _OPTIONAL_EXAMPLES.get(key)
        differences.extend(_native_differences(f"{prefix}.{key}", stored, observed, example))
    return differences


def _inspect_queue(snapshot, job, root, expected_target, result, check, require_live_queue):
    """Revalidate one explicit original queue, never discover or create state."""
    schema = snapshot["schema"]
    declared = snapshot.get("queue_identity")
    if schema < 4:
        check("schema_lineage", declared is None and job.get("node_submission") is None
              and (isinstance(snapshot.get("controller", {}), dict)
                   and snapshot.get("controller", {}).get("schema", schema) == schema),
              "Enrolled schema-4 evidence must not be downgraded to a legacy snapshot")
    needs_live = require_live_queue or schema == 4 or declared is not None or expected_target is not None
    if root is not None and not needs_live:
        # Historical schema-2/3 local evidence may never have included a DB.
        # Only a confirmed absence qualifies for that route; permission failures
        # and a database which disappears after detection require verification.
        try:
            (root / "queue.sqlite3").lstat()
        except FileNotFoundError:
            pass
        except OSError:
            needs_live = True
        else:
            needs_live = True
    actual = None
    identity_matches = None
    unavailable = False
    if needs_live and root is not None:
        from cmw.jobs.lifecycle import JobObservationConflict, JobObservationUnavailable, read_job_with_identity_retry
        from cmw.jobs.store import JobsError, Store

        differences = []
        live = result["live_queue"] = {"status": "MATCH", "differences": differences}
        try:
            current, control, actual = read_job_with_identity_retry(Store(root), job["id"])
        except JobObservationUnavailable as exc:
            unavailable = True
            live.update(status="UNAVAILABLE", reason=str(exc))
            check("native_queue", None, f"Original queue unavailable after bounded read retries: {exc}")
        except JobObservationConflict as exc:
            controller = snapshot.get("controller")
            stored = {"job.id": job["id"], "schema": schema, "queue_identity": declared,
                      "controller.runtime_protocol": controller.get("runtime_protocol", 3)
                      if isinstance(controller, dict) else 3}
            examples = {"job.id": 0, "schema": 0, "queue_identity": _ENROLLMENT_EXAMPLE,
                        "controller.runtime_protocol": 0}
            for name, value in exc.fields.items():
                differences.extend(_native_differences(name, stored[name], value, examples[name]))
            if exc.location is not None:
                enrollment = exc.fields["queue_identity"]
                differences.extend(_native_differences("queue_location", _selected_fields(enrollment, exc.location),
                                                       exc.location, _LOCATION_EXAMPLE))
            if not differences:
                differences.append({"field": "queue.structure", "stored": "valid native record", "observed": str(exc)})
            live.update(status="CONTRADICTED", reason=str(exc))
            check("native_queue", False, str(exc))
        except (JobsError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
            live.update(status="CONTRADICTED", reason=str(exc))
            differences.append({"field": "queue.structure", "stored": "valid native record", "observed": str(exc)})
            check("native_queue", False, f"Original queue contradicts the required native format: {exc}")
        else:
            schema_diffs = field_differences("schema", schema, control["schema"])
            differences.extend(schema_diffs)
            check("native_queue_schema", not schema_diffs,
                  "Snapshot schema must match its original native queue schema")
            job_diffs = _native_field_differences("job", job, current, _JOB_FIELDS)
            # A missing enrollment and an empty legacy projection both supplied
            # null for every compared suspension field in earlier records.
            suspensions = [item.get("suspension") or {} for item in (job, current)]
            saved, observed = suspensions
            if isinstance(saved, dict) and isinstance(observed, dict):
                job_diffs.extend(_native_field_differences("job.suspension", saved, observed, _SUSPENSION_FIELDS))
            else:
                job_diffs.extend(_native_differences("job.suspension", saved, observed, {}))
            differences.extend(job_diffs)
            check("native_attempt", not job_diffs, "Saved invocation must match the selected original native attempt")
            if declared is not None or (schema < 4 and actual is not None):
                identity_diffs = _native_differences("queue_identity", declared, actual, _ENROLLMENT_EXAMPLE)
                differences.extend(identity_diffs)
                identity_matches = not identity_diffs
            if differences:
                live["status"] = "CONTRADICTED"
    if schema == 4:
        check("queue_identity", identity_matches,
              "Schema-4 completion needs matching exported original enrollment; re-export older snapshots missing queue_identity")
        if declared is not None:
            result["binding"]["queue_identity"] = declared
    identity = declared if unavailable else actual
    if job.get("node_submission") is not None:
        submission = job["node_submission"]
        valid = (isinstance(submission, dict) and type(submission.get("schema")) is int
                 and submission["schema"] == 1
                 and (identity is None or isinstance(identity, dict)
                      and all(submission.get(key) == identity.get(key) for key in ("node_id", "queue_id")))
                 and all(isinstance(submission.get(key), str) and re.fullmatch(pattern, submission[key])
                         for key, pattern in (("request_id", r"[a-f0-9]{32}"), ("digest", r"[a-f0-9]{64}"))))
        check("node_submission", None if valid and unavailable else bool(valid and actual is not None),
              "Recorded node request must match the original enrollment and exact request identity")
        result["binding"]["node_submission"] = submission
    if expected_target is not None:
        required = {"node_id", "queue_id", "job_id", "attempt_id"}
        valid = (isinstance(expected_target, dict) and required <= set(expected_target)
                 and not set(expected_target) - required - {"host_id", "runtime_id"}
                 and type(expected_target.get("job_id")) is int
                 and expected_target["job_id"] == job["id"]
                 and expected_target.get("attempt_id") == job["attempt_id"])
        if valid and identity is not None:
            valid = isinstance(identity, dict) and all(expected_target[key] == identity.get(key)
                for key in ("node_id", "queue_id", "host_id") if key in expected_target)
        check("owner_target", None if valid and unavailable else bool(valid and actual is not None),
              "Review target must match the original node, queue and operational attempt")
        if isinstance(expected_target, dict) and "runtime_id" in expected_target:
            submission = job.get("node_submission")
            runtime = submission.get("runtime_id") if isinstance(submission, dict) else None
            check("submission_runtime", expected_target["runtime_id"] == runtime if runtime else None,
                  "Requested runtime identity needs matching recorded submission-runtime evidence")


def inspect_execution(snapshot_path, receipt_path, *, job_id, attempt_id, run_directory,
                      runner_record=None, native_sources=(), expected_target=None, require_live_queue=False):
    """Inspect explicit saved records; retain their complete file source identities."""
    return _inspect_execution(snapshot_path, receipt_path, job_id=job_id, attempt_id=attempt_id,
                              run_directory=run_directory, runner_record=runner_record,
                              native_sources=native_sources, expected_target=expected_target,
                              require_live_queue=require_live_queue)


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
