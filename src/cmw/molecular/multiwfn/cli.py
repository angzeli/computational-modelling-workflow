"""Machine-readable shell bridge for the Multiwfn runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cmw.core.process_health import format_status_line
from cmw.core.provenance import atomic_write_json

from .automation import Operation, menu_stream
from .runtime import parallelism_warning, prepare_runtime, sample_health


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _prepare(args: argparse.Namespace) -> int:
    runtime = prepare_runtime(
        attempt_directory=args.attempt_directory,
        executable=args.multiwfn_exe,
        settings_source=args.settings_source,
        cli_threads=args.threads,
    )
    atomic_write_json(args.metadata, {"schema_version": 1, "runtime": runtime.to_dict()})
    _print(runtime.to_dict())
    return 0


def _stream(args: argparse.Namespace) -> int:
    parameters = json.loads(args.parameters)
    if not isinstance(parameters, dict):
        raise ValueError("automation parameters must be a JSON object")
    print(menu_stream(Operation(args.operation), args.version, parameters), end="")
    return 0


def _health(args: argparse.Namespace) -> int:
    report, _ = sample_health(args.pid, args.state, args.activity)
    observation = parallelism_warning(args.threads, report)
    result = {
        "state": report.assessment.state.value,
        "reason": report.assessment.reason,
        "status_line": format_status_line(report),
        "effective_cores": observation.effective_cores,
        "warning": observation.warning,
        "diagnostic_only": True,
        "process_count": report.snapshot.process_count,
        "aggregate_cpu_percent": report.snapshot.cpu_percent,
        "aggregate_rss_bytes": report.snapshot.rss_bytes,
        "cumulative_cpu_seconds": report.snapshot.cumulative_cpu_seconds,
    }
    if args.history is not None:
        args.history.parent.mkdir(parents=True, exist_ok=True)
        with args.history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
    _print(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--attempt-directory", type=Path, required=True)
    prepare.add_argument("--metadata", type=Path, required=True)
    prepare.add_argument("--multiwfn-exe")
    prepare.add_argument("--settings-source", type=Path)
    prepare.add_argument("--threads")
    prepare.set_defaults(handler=_prepare)
    stream = sub.add_parser("stream")
    stream.add_argument("--operation", choices=[item.value for item in Operation], required=True)
    stream.add_argument("--version", required=True)
    stream.add_argument("--parameters", required=True)
    stream.set_defaults(handler=_stream)
    health = sub.add_parser("health")
    health.add_argument("--pid", type=int, required=True)
    health.add_argument("--state", type=Path, required=True)
    health.add_argument("--activity", type=Path, action="append", default=[])
    health.add_argument("--threads", type=int, required=True)
    health.add_argument("--history", type=Path)
    health.set_defaults(handler=_health)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        _print({"error": type(exc).__name__, "reason": str(exc)})
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
