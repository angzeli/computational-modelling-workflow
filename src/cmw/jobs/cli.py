"""Argparse integration, with lazy optional console imports."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unicodedata

from .store import JobsError, Store


def safe_text(value):
    text = str(value)
    # Strip entire CSI/OSC sequences, then remaining control/format characters.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


def elapsed(value):
    if value is None:
        return "—"
    seconds = int(value)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def tail(path, limit=16384):
    if limit < 1 or limit > 1024 * 1024:
        raise JobsError("Tail bytes must be between 1 and 1048576")
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - limit))
            return safe_text(handle.read(limit).decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return "No log yet."


def status_text(state):
    control = state["controller"]
    online = "Stale" if control["stale"] else "Online" if control["online"] else "Offline"
    lines = ["CMW / JOBS — local", f"Controller: {online}   Dispatch: {'ON' if control['dispatch'] else 'OFF'}   Mode: Sequential",
             "ORDER  JOB ID      NAME                  ENGINE      STATUS       CPUS  ELAPSED     REASON"]
    for job in state["jobs"]:
        lines.append(f"{str(job['order'] or '—'):>5}  {job['display_id']:<10}  {safe_text(job['name'])[:20]:20}  {safe_text(job['engine'])[:10]:10}  "
                     f"{job['status']:11}  {str(job['resources']['cpus'] or '—'):>4}  {elapsed(job['elapsed']):>10}  {safe_text(job['reason'])}")
    if not state["jobs"]:
        lines.append("No jobs. Add a prepared command with cmw jobs add, then explicitly start.")
    lines.append(f"State: {state['state_directory']}")
    return "\n".join(lines)


def register(subcommands):
    parser = subcommands.add_parser("jobs", help="local sequential queue and terminal console")
    parser.add_argument("--state", type=Path, help="isolated state directory (or CMW_JOBS_STATE)")
    operations = parser.add_subparsers(dest="jobs_operation", required=True)
    for name in ("status", "start", "stop", "pause", "resume", "watch"):
        command = operations.add_parser(name)
        if name != "watch":
            command.add_argument("--json", action="store_true")
        command.set_defaults(handler=handle)
    add = operations.add_parser("add", help="enqueue trusted foreground argv; never starts execution")
    add.add_argument("--name", required=True)
    add.add_argument("--engine", default="Command")
    add.add_argument("--cwd", required=True, type=Path)
    add.add_argument("--cpus", type=int)
    add.add_argument("--memory-gib", type=float)
    add.add_argument("--mpi-ranks", type=int)
    add.add_argument("--threads-per-rank", type=int)
    add.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    add.add_argument("--on-failure", choices=("pause", "continue"), default="pause")
    add.add_argument("--hold", action="store_true")
    add.add_argument("--layout", type=Path, help="link and verify an existing CMW layout; does not bypass its runner")
    add.add_argument("--json", action="store_true")
    add.add_argument("argv", nargs="...")
    add.set_defaults(handler=handle)
    for name in ("show", "logs", "hold", "release", "move", "cancel"):
        command = operations.add_parser(name)
        command.add_argument("job_id")
        command.add_argument("--json", action="store_true")
        if name == "move":
            command.add_argument("position", type=int)
        if name == "cancel":
            command.add_argument("--yes", action="store_true", help="confirm termination of a running managed job")
        if name == "logs":
            command.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
            command.add_argument("--bytes", type=int, default=16384)
        command.set_defaults(handler=handle)


def handle(args):
    store = Store(args.state)
    operation = args.jobs_operation
    try:
        if operation == "watch":
            try:
                from .tui import JobsApp
            except ImportError as exc:
                raise JobsError('Console dependency missing; install with: pip install -e ".[jobs]"') from exc
            JobsApp(store).run()
            return 0
        if operation == "add":
            environment = {}
            for entry in args.env:
                if "=" not in entry:
                    raise JobsError("--env requires NAME=VALUE")
                key, value = entry.split("=", 1)
                environment[key] = value
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            result = store.add(argv=argv, cwd=args.cwd, name=args.name, engine=args.engine,
                cpus=args.cpus, memory_gib=args.memory_gib, mpi_ranks=args.mpi_ranks,
                threads_per_rank=args.threads_per_rank, env=environment, hold=args.hold,
                on_failure=args.on_failure, layout=args.layout)
        elif operation in {"start", "stop"}:
            from . import runtime
            getattr(runtime, operation)(store)
            result = store.snapshot()
        elif operation in {"pause", "resume"}:
            store.dispatch(operation == "resume")
            result = store.snapshot()
        elif operation in {"hold", "release", "move", "cancel"}:
            result = store.change(args.job_id, operation, position=getattr(args, "position", None), confirm=getattr(args, "yes", False))
        elif operation == "status":
            result = store.snapshot()
        else:
            state = store.snapshot()
            candidates = [j for j in state["jobs"] if str(args.job_id) in {str(j["id"]), f"J{j['id']}", j["display_id"]}]
            if not candidates:
                raise JobsError(f"No such job: {args.job_id}")
            result = candidates[0]
            if operation == "logs":
                result = {"job_id": result["display_id"], "stream": args.stream,
                          "text": tail(result["logs"][args.stream], args.bytes)}
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        elif "jobs" in result:
            print(safe_text(status_text(result)))
        elif "text" in result:
            print(result["text"])
        else:
            print(safe_text(json.dumps(result, indent=2, ensure_ascii=False)))
        return 0
    except (JobsError, OSError) as exc:
        payload = {"status": "FAILED", "code": "JOBS_ERROR", "reason": str(exc), "exit_code": 2}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=True))
        else:
            print(f"cmw jobs: {safe_text(exc)}", file=sys.stderr)
        return 2
