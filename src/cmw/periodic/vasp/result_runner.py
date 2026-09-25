"""Bounded interpretation of an external run-vasp.sh text record.

The dialect name versions CMW's interpretation of an unversioned local record;
it does not authenticate the runner, or turn its scientific summaries into facts.
Only the observed single-image, no-restart, managed-foreground invocation is
supported. Relocated output trees and archived copies are intentionally excluded.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import re

from .result_sources import SnapshotReader, SourceSnapshotError


DIALECT = "vasp-run-metadata-text-v1"
_INPUTS = ("INCAR", "POSCAR", "POTCAR", "KPOINTS")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STAGED = re.compile(
    r"(INCAR|POSCAR|POTCAR|KPOINTS); bytes=([0-9]+); "
    r"source_stat=\(([0-9]+), ([0-9]+), ([0-9]+), ([0-9]+)\); SHA256=([0-9a-f]{64})\Z")
_KEYS = frozenset({
    "state", "child_status", "staging_start_utc", "source", "output", "donor", "binary", "sha",
    "parallel", "origins", "active_opt", "istart", "icharg", "variant", "requested_version",
    "launcher_repository", "input_repository", "mpi", "command", "threads", "hard_budget_seconds",
    "stop_before_seconds", "managed_foreground", "restart_requested", "restart_preflight", "warnings",
    "incar_overrides", "staged_file", "original_incar_sha256", "effective_incar_sha256", "status",
    "reason", "start_utc", "hard_deadline_utc", "end_utc", "elapsed_s", "pid", "advance_stop",
    "observed_version", "normal_footer", "scientific_convergence", "restart_observed", "energy_eV",
    "nelect", "finite_observables", "checkpoint", "launcher_status",
})
_REPEATED = {"state", "child_status", "staged_file", "checkpoint"}


def validate_declaration(value):
    if (not isinstance(value, dict) or set(value) != {"dialect", "path", "sha256"}
            or value.get("dialect") != DIALECT
            or not isinstance(value.get("path"), str) or not value["path"].strip()
            or "\0" in value["path"] or not isinstance(value.get("sha256"), str)
            or _DIGEST.fullmatch(value["sha256"]) is None):
        raise ValueError("runner_record requires the supported dialect, explicit path and lowercase SHA-256")
    return value


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _absolute(value):
    _require(isinstance(value, str) and bool(value) and not any(ord(c) < 32 for c in value),
             "Runner path must be a nonempty absolute path without control characters")
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "Runner paths must be absolute without traversal")
    _require(path == path.resolve(), "Runner paths must retain their original canonical location")
    return path


def _timestamp(value):
    _require(isinstance(value, str), "Runner UTC timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed),
             "Runner timestamps must explicitly record UTC")
    result = parsed.timestamp()
    _require(math.isfinite(result) and result >= 0, "Runner timestamp must be finite and nonnegative")
    return result


def _command(argv):
    _require(isinstance(argv, list) and argv and all(isinstance(a, str) and "\0" not in a for a in argv),
             "Expected an explicit runner argv list")
    executable = _absolute(argv[0])
    _require(executable.name == "run-vasp.sh", "Only the explicit run-vasp.sh invocation is supported")
    pairs = {"--input", "--output", "--binary", "--ranks", "--ncore", "--kpar", "--mpi-mode",
             "--restart", "--timeout", "--stop-before"}
    options = {}
    index = 1
    while index < len(argv):
        name = argv[index]
        _require(name not in options, "Repeated runner arguments are ambiguous")
        if name == "--managed-foreground":
            options[name] = True
            index += 1
        else:
            _require(name in pairs and index + 1 < len(argv), "Unsupported runner argument format")
            options[name] = argv[index + 1]
            index += 2
    _require(set(options) == pairs | {"--managed-foreground"}, "Runner invocation must explicitly record supported options")
    _require(options["--restart"] == "none" and options["--binary"] == "std"
             and options["--mpi-mode"] in {"synthetic", "native"}, "Unsupported runner mode")
    for key in ("--ranks", "--ncore", "--kpar"):
        _require(re.fullmatch(r"[1-9][0-9]*", options[key]) is not None, "Invalid runner parallel count")
    ranks, ncore, kpar = (int(options[k]) for k in ("--ranks", "--ncore", "--kpar"))
    # The observed external runner explicitly restricts launches to 1..8 ranks.
    _require(ranks <= 8 and ranks % kpar == 0 and ranks // kpar % ncore == 0,
             "Unsupported runner parallel decomposition")
    timeout, stop = float(options["--timeout"]), float(options["--stop-before"])
    _require(math.isfinite(timeout) and math.isfinite(stop) and 0 <= stop < timeout,
             "Runner budgets must be finite and ordered")
    return _absolute(options["--input"]), _absolute(options["--output"])


def inspect_runner(declaration, job, receipt, run_directory, native_sources=()):
    """Read metadata and four runtime input identities; never retain their text."""
    result = {"valid": False, "facts": None, "sources": [], "error": None}

    def consume(path, role, *, text=False, max_bytes=64 * 1024 * 1024):
        reader = SnapshotReader(path, role, max_bytes=max_bytes)
        result["sources"].append(reader.record)
        with reader:
            if text:
                content = "".join(line.text for line in reader)
            else:
                for _line in reader:
                    pass
                content = None
        _require(reader.record["stable"] and reader.record["coverage"] == "complete",
                 "Runner source changed or was incompletely observed")
        return content, reader.record

    try:
        validate_declaration(declaration)
        source, output = _command(job.get("argv"))
        _require(_absolute(job.get("cwd")) == source, "Jobs wrapper cwd must equal the runner's explicit input")
        _require(_absolute(str(run_directory)) == output, "Native run must occupy the exact recorded runner output")
        path = _absolute(declaration["path"])
        _require(path == output / "RUN_METADATA.txt", "Runner record must occupy its original runtime location")
        content, record = consume(path, "runner_record", text=True, max_bytes=64 * 1024)
        _require(record["sha256"] == declaration["sha256"], "Runner record differs from its declared identity")
        values, positions, staged = {}, {}, {}
        lines = content.splitlines()
        for index, line in enumerate(lines):
            key, separator, value = line.partition(": ")
            _require(separator and key in _KEYS, "Unsupported runner metadata line or key")
            _require(key in _REPEATED or key not in values, "Repeated runner metadata identity or terminal key")
            values.setdefault(key, []).append(value)
            positions.setdefault(key, []).append(index)
            if key == "staged_file":
                match = _STAGED.fullmatch(value)
                _require(match is not None, "Malformed staged-file identity")
                name, size, _device, _inode, stat_size, _mtime, digest = match.groups()
                _require(name not in staged and int(size) == int(stat_size) and int(size) > 0,
                         "Duplicate, empty or inconsistent staged-file identity")
                staged[name] = {"sha256": digest, "bytes": int(size)}
        scalar = {key: entries[0] for key, entries in values.items() if key not in _REPEATED}
        _require(values.get("state") == ["STAGING", "RUNNING", "FINISHED"]
                 and positions["state"][0] == 0 and positions["state"][-1] == len(lines) - 1,
                 "Expected one complete ordered STAGING/RUNNING/FINISHED lifecycle")
        start, running, finished = positions["state"]
        _require(values.get("child_status") == ["NOT STARTED", "0"]
                 and start < positions["child_status"][0] < running
                 and running < positions["child_status"][1] < finished,
                 "Expected one not-started then successful child status")
        _require(set(staged) == set(_INPUTS), "Exactly four staged input identities are required")
        _require(all(start < index < running for index in positions["staged_file"]),
                 "Input staging must precede payload execution")
        for key in ("staging_start_utc", "source", "output", "managed_foreground", "restart_requested",
                    "requested_version", "original_incar_sha256", "effective_incar_sha256"):
            _require(key in positions and start < positions[key][0] < running,
                     "Runner staging identities must precede payload execution")
        for key in ("start_utc", "end_utc"):
            _require(key in positions and running < positions[key][0] < finished,
                     "Runner completion times must occur in the terminal record block")
        _require(scalar.get("source") == str(source) and scalar.get("output") == str(output),
                 "Metadata source/output must match the exact Jobs argv paths")
        _require(scalar.get("managed_foreground") == "True" and scalar.get("restart_requested") == "none"
                 and scalar.get("requested_version") == "6.6.1", "Unsupported runner metadata mode")
        for key, expected in (("status", "0"), ("launcher_status", "0"), ("reason", "completed"),
                              ("advance_stop", "not reached")):
            _require(scalar.get(key) == expected and running < positions[key][0] < finished,
                     "Runner must complete successfully without a stop or timeout")
        times = {key: _timestamp(scalar.get(key)) for key in ("staging_start_utc", "start_utc", "end_utc")}
        chain = [job.get("started_at"), times["staging_start_utc"], times["start_utc"],
                 times["end_utc"], receipt.get("recorded_at"), job.get("finished_at")]
        _require(all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in chain)
                 and chain == sorted(chain), "Runner lifecycle must lie within the exact Jobs attempt and receipt")
        effective = scalar.get("effective_incar_sha256")
        _require(isinstance(effective, str) and _DIGEST.fullmatch(effective) is not None
                 and scalar.get("original_incar_sha256") == staged["INCAR"]["sha256"],
                 "Original and effective INCAR identities must be explicit and consistent")
        identities = {}
        for name in _INPUTS:
            _content, observed = consume(output / name, "runner_input:" + name)
            expected_hash = effective if name == "INCAR" else staged[name]["sha256"]
            _require(observed["sha256"] == expected_hash, "Runtime " + name + " differs from the runner's input identity")
            if name != "INCAR":
                _require(observed["bytes_read"] == staged[name]["bytes"], "Runtime input size differs from staging")
            identities[name] = {"sha256": observed["sha256"], "bytes": observed["bytes_read"]}
        _require(isinstance(native_sources, (list, tuple))
                 and all(isinstance(item, dict) for item in native_sources), "Malformed native input source observations")
        for name in ("POSCAR", "INCAR"):
            native = [s for s in native_sources if s.get("role") == name]
            if native_sources:
                _require(len(native) == 1 and native[0].get("stable") is True
                         and native[0].get("coverage") == "complete"
                         and native[0].get("resolved_path") == str(output / name)
                         and native[0].get("sha256") == identities[name]["sha256"]
                         and native[0].get("bytes_read") == identities[name]["bytes"],
                         "Runner input identity differs from consumed native " + name)
        result["facts"] = {
            "dialect": DIALECT, "metadata_sha256": record["sha256"], "wrapper_cwd": str(source),
            "payload_cwd": str(output), "input_identities": identities,
            "original_incar_sha256": scalar["original_incar_sha256"], "staged_input_identities": staged,
            "lifecycle": values["state"], "times": times,
            "launcher_repository": scalar.get("launcher_repository"),
            "limitations": ["Local unversioned runner text and retrospective declarations are not authenticated.",
                            "Runner code at execution, including dirty changes, is not authenticated by a current source checkout.",
                            "Input byte identity does not validate potentials, k-point adequacy or scientific convergence."],
        }
        result["valid"] = True
    except (SourceSnapshotError, ValueError, TypeError, OverflowError, OSError) as exc:
        result["error"] = str(exc)
    return result
