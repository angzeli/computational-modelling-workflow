"""Argparse integration, with lazy optional console imports."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
import unicodedata

from .store import JobsError, Store


def safe_text(value):
    text = str(value)
    # Strip entire CSI/OSC sequences, then remaining control/format characters.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


def name_text(value):
    """Literal single-line presentation; the persisted name remains unchanged."""
    return " ".join(safe_text(value).replace("\t", " ").splitlines())


def elapsed(value):
    if value is None:
        return "—"
    seconds = int(value)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def metric(value, quality, pattern, *, stale=False):
    if value is None:
        return "…" if quality == "warming-up" else "—"
    marker = "~" if stale else "*" if quality == "partial" else "?" if quality == "unavailable" else ""
    return pattern.format(value) + marker


def usage_cells(usage):
    usage = usage or {}
    cores = usage.get("cpu_cores")
    cpu = metric(None if cores is None else cores * 100, usage.get("cpu_quality"), "{:.0f}%", stale=usage.get("stale", False))
    rss = usage.get("rss_bytes")
    memory = metric(None if rss is None else rss / 2**30, usage.get("memory_quality"), "{:.1f} GiB RSS", stale=usage.get("stale", False))
    return cpu, memory


def usage_detail(usage):
    if not usage:
        return "CPU NOW: —   RAM NOW: — (no current workload sample)"
    cpu, memory = usage_cells(usage)
    age = usage.get("age_seconds")
    return (f"CPU NOW: {cpu} (100% = one logical CPU)   RAM NOW: {memory} (aggregate RSS)\n"
            f"Measurement age: {'—' if age is None else f'{age:.1f}s'}{' STALE' if usage.get('stale') else ''}\n"
            f"CPU quality: {usage.get('cpu_quality', 'unavailable')} ({usage.get('cpu_members', 0)}/{usage.get('total_members', 0)} members); "
            f"RAM quality: {usage.get('memory_quality', 'unavailable')} ({usage.get('memory_members', 0)}/{usage.get('total_members', 0)} members)\n"
            "RSS sums may count shared pages more than once; usage is not requested resources.\n"
            f"{usage.get('reason') or ''}")


def machine_text(usage):
    usage = usage or {}
    cpu = metric(usage.get("cpu_percent"), usage.get("cpu_quality"), "{:.0f}%", stale=usage.get("stale", False))
    used, total = usage.get("ram_used_bytes"), usage.get("ram_total_bytes")
    memory = "—" if used is None or total is None else f"{used / 2**30:.1f} / {total / 2**30:.1f} GiB"
    if usage.get("stale"):
        memory += "~"
    warm = " (warming up)" if usage.get("cpu_quality") == "warming-up" else ""
    return f"Machine: CPU {cpu}{warm}   RAM {memory}"


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


def _unknown_worker_observation(owner):
    from .lifecycle import observe_process

    return observe_process(owner).to_dict()


def _unknown_lock_observation(store, attempt_id):
    from .lifecycle import observe_lock

    return observe_lock(store, attempt_id).to_dict()


def _unknown_ownership_observation(store, job, *, observation=None):
    """Compatibility projection of the shared read-only lifecycle evidence."""
    if observation is not None:
        return {'advisory': True, 'observed_at': observation['observed_at'],
                'worker': observation['evidence']['worker'],
                'worker_lock': observation['evidence']['worker_lock']}
    return {'advisory': True, 'observed_at': time.time(),
            'worker': _unknown_worker_observation(job.get('worker')),
            'worker_lock': _unknown_lock_observation(store, job.get('attempt_id'))}


def execution_text(observation):
    """Describe stored state and narrow operational evidence separately."""
    evidence = observation.get('evidence', {})
    lines = [f"Job: {observation.get('job_id')}",
             f"Persisted status: {observation['persisted_status']}",
             f"Execution observation: {observation['observed_execution']}",
             f"Quality: {observation['observation_quality']}",
             f"Observation age: {observation.get('age_seconds', 0):.1f}s"
             + (' STALE' if observation.get('stale') else '')]
    for key, label in (('worker', 'Worker'), ('worker_lock', 'Worker lock'),
                       ('domain', 'Process domain'), ('receipt', 'Receipt'),
                       ('controller', 'Controller')):
        item = evidence.get(key, {})
        lines.append(f"{label}: {item.get('verdict', 'unavailable')} — {item.get('reason', '')}")
    suspension = evidence.get('suspension', {})
    if suspension and suspension.get('verdict') != 'not_applicable':
        flag = suspension.get('inhibited')
        inhibited = 'yes' if flag is True else 'no' if flag is False else 'unknown'
        lines.append(f"Stored suspension: {suspension.get('phase', 'unknown')} · "
                     f"desired {suspension.get('desired', 'unknown')} · inhibited {inhibited}")
        lines.append(f"Suspension evidence: {suspension.get('verdict', 'unavailable')} — "
                     f"{suspension.get('reason', '')}")
    lines.extend([f"Conclusion: {observation['summary']}",
                  'Reconciliation: ' + ('Review required.' if observation['reconciliation_required'] else 'No action indicated.')])
    lines.extend(f"Limit: {item}" for item in observation.get('limitations', []))
    return '\n'.join(lines)


def _managed_job(store, job_id):
    from .lifecycle import read_job

    if str(job_id).startswith('E'):
        raise JobsError('External observations are read-only and cannot be reconciled or adopted as managed jobs')
    return read_job(store, job_id)


def inspect_lifecycle(store, args):
    from .lifecycle import observe_execution

    job, _ = _managed_job(store, args.job_id)
    result = observe_execution(store, job).to_dict()
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False)
          if args.json else safe_text(execution_text(result)))
    return 0


def inspect_reconciliation(store, args):
    if args.apply:
        raise JobsError('P2 lifecycle recovery apply not implemented/qualified. Reconciliation --apply is unavailable; use --preview.')
    from .reconciliation import preview_reconciliation, preview_text

    job, control = _managed_job(store, args.job_id)
    result = preview_reconciliation(store, job, state={'schema': control['schema'], 'controller': control}).to_dict()
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False)
          if args.json else safe_text(preview_text(result)))
    return 0


def guard_text(state):
    guard = state.get("external_activity")
    if not guard:
        return "Guard: UNAVAILABLE — observation pending; admission blocked"
    admission = state.get("admission", {})
    age = elapsed(guard.get("age_seconds"))
    previous = state.get("controller_guard")
    controller_note = (f"\nController last evaluation: {previous.get('state', 'UNAVAILABLE')} at {previous.get('observed_at', 'unknown')} (separate from client scan)"
                       if previous else "")
    return (f"Admission: {'AWAITING CONTROLLER CHECK' if admission.get('permitted') else 'BLOCKED'} — {admission.get('reason', guard['reason'])}\n"
            f"Guard: {guard['state']} — {guard['reason']}  Scan age: {age}{' STALE' if guard.get('stale') else ''}\n"
            f"Scope: {guard.get('scope', 'current local user / recognized accessible executables')}  Source: {guard.get('source', 'client')}"
            + controller_note
            + ("\nCONFLICT: managed and external activity overlap; neither is interrupted." if admission.get('conflict') else "")
            + "".join(f"\nCoverage: {warning}" for warning in guard.get('coverage', {}).get('warnings', [])))


def sharing_text(state, *, compact=False):
    decision = state.get("admission", {})
    if state.get("mode", decision.get("mode")) != "Bounded Sharing":
        return ""
    number = lambda value: "—" if value is None else f"{value:g}" if isinstance(value, (int, float)) else str(value)
    committed = (f"Committed: CPU {number(decision.get('cpu_committed'))}/{number(decision.get('cpu_budget'))}   "
                 f"RAM {number(decision.get('memory_committed_gib'))}/{number(decision.get('memory_budget_gib'))} GiB")
    if compact:
        return committed + (" !" if decision.get('warnings') else "")
    anchor = decision.get('anchor_primary')
    if anchor is None:
        anchor = next((job.get('sharing_anchor') for job in state.get('jobs', []) if job.get('sharing_anchor') and job['status'] in {'Starting', 'Run', 'Cancelling', 'Unknown'}), None)
    if isinstance(anchor, dict):
        anchor = anchor.get('id', anchor)
    available = decision.get('host_memory_available_bytes')
    return (committed + f"\nAuxiliary slot: {decision.get('auxiliary_slot', '—')}   Anchor: {anchor or '—'}\n"
            f"External reservation: {decision.get('external_reservation_state', 'none')}   Write scopes: {decision.get('write_scope_result', '—')}\n"
            + "".join(f"Sharing warning: {warning}\n" for warning in decision.get('warnings', []))
            + f"Host CPU headroom: {number(decision.get('host_cpu_headroom'))} logical CPUs   "
            f"Available RAM: {'—' if available is None else f'{available / 2**30:.1f} GiB'}")


def job_sharing_detail(job):
    scheduling = job.get('scheduling', {})
    return (f"Role: {scheduling.get('role', 'primary').title()}   Auxiliary coexistence: {'Allowed' if scheduling.get('allow_auxiliary') else 'Exclusive'}\n"
            f"Independent: {bool(scheduling.get('independent'))}   Resource contract: {scheduling.get('resource_contract') or 'none'}\n"
            f"Write scope: {scheduling.get('write_scope') or job.get('cwd') or '—'}\n"
            f"Sharing anchor: {job.get('sharing_anchor') or '—'}")


def observation_sharing(state):
    return {'reservation': state.get('scheduler', {}).get('external_reservation'),
            'reservation_state': state.get('admission', {}).get('external_reservation_state', 'none'),
            'admission_reason': state.get('admission', {}).get('reason')}


def external_detail(observation):
    return (f"EXTERNAL OBSERVATION: {observation['id']} — READ ONLY\n"
            f"Ownership: {observation.get('ownership', 'External / unattributed')} — not managed by this queue\n"
            f"Engine: {observation['engine']}   PID: {observation['pid']}   Observed OS state: {observation.get('status', '—')}\n"
            f"NPROC (observed processes): {observation.get('nproc', 1)}   Process age: {elapsed(observation.get('age_seconds'))}\n"
            f"{usage_detail(observation.get('usage'))}\n"
            f"Executable: {observation.get('exe') or '—'}\nWorking directory: {observation.get('cwd') or '—'}\n"
            "CPUs requested: —   RAM requested: —   Scientific status: Not evaluated\n"
            f"Evidence: {json.dumps(observation.get('evidence', []), ensure_ascii=False)}\n"
            f"Selected queue sharing declaration (not process ownership): {json.dumps(observation.get('scheduling_context'), ensure_ascii=False)}\n"
            "Observation only. No cancel, hold, reorder, retry, or logs.\n"
            + json.dumps(observation, indent=2, ensure_ascii=False))


def status_text(state):
    control = state["controller"]
    online = "Stale" if control["stale"] else "Online" if control["online"] else "Offline"
    lines = ["CMW / JOBS — local", f"Controller: {online}   Dispatch: {'ON' if control['dispatch'] else 'OFF'}   Mode: {state.get('mode', 'Sequential')}",
             machine_text(state.get("machine_usage")),
             "ORDER  JOB ID      NAME  ENGINE      STATUS       CPUS  CPU NOW  RAM NOW          ELAPSED     REASON"]
    if sharing_text(state):
        lines.insert(3, sharing_text(state))
    from .suspension_presentation import effective_status, suspension_summary
    for job in state["jobs"]:
        cpu_now, ram_now = usage_cells(job.get("usage"))
        lines.append(f"{str(job['order'] or '—'):>5}  {job['display_id']:<10}  {name_text(job['name'])}  {safe_text(job['engine'])[:10]:10}  "
                     f"{effective_status(job):11}  {str(job['resources']['cpus'] or '—'):>4}  {cpu_now:>7}  {ram_now:>15}  {elapsed(job['elapsed']):>10}  {safe_text(job['reason'])}  ROLE {job.get('scheduling', {}).get('role', 'primary')}")
        if suspension_summary(job):
            lines.append("       " + suspension_summary(job))
    if not state["jobs"]:
        lines.append("No jobs. Add a prepared command with cmw jobs add, then explicitly start.")
    lines.append(guard_text(state))
    lines.append("EXTERNAL ACTIVITY — READ ONLY (NPROC = observed processes; AGE = process age)")
    for item in state.get("external_activity", {}).get("observations", []):
        cpu_now, ram_now = usage_cells(item.get("usage"))
        lines.append(f"{item['id']}  {item['engine']}  PID {item['pid']}  NPROC {item.get('nproc', 1)}  "
                     f"OS {item.get('status', '—')}  CPU NOW {cpu_now}  RAM NOW {ram_now}  AGE {elapsed(item.get('age_seconds'))}  {item.get('exe') or '—'}")
    lines.append("CPU NOW: 100% = one logical CPU; RAM NOW: aggregate RSS (shared pages may repeat). … warming up; * partial; ~ stale; ? unavailable cached value.")
    lines.append("Best-effort pre-dispatch observation; no machine-wide reservation or exclusion.")
    lines.append(f"State: {state['state_directory']}")
    return "\n".join(lines)


def register(subcommands):
    parser = subcommands.add_parser("jobs", help="local queue, optional bounded sharing and terminal console",
        description="Local queue: Sequential by default; opt-in Bounded Sharing permits one Primary plus at most one Auxiliary. "
                    "Telemetry may veto admission but never proves completion or releases commitments. "
                    "Resource declarations are not hard OS limits; external activity observation provides no machine-wide exclusion.")
    parser.add_argument("--state", type=Path, help="isolated state directory (or CMW_JOBS_STATE)")
    operations = parser.add_subparsers(dest="jobs_operation", required=True)
    for name in ("status", "start", "stop", "pause", "resume", "watch"):
        command = operations.add_parser(name)
        if name == "watch":
            command.description = ("Observe Jobs without starting execution. Enter: details; F: files; N: edit a managed job note. "
                "Editor: Ctrl-S saves, Esc cancels, Ctrl-C detaches without saving; Q inserts text. "
                "U opens selected-job Suspend/Continue readiness and confirmation. Outside text input Q or Ctrl-C detaches with execution state unchanged.")
        if name != "watch":
            command.add_argument("--json", action="store_true")
        command.set_defaults(handler=handle)
    add = operations.add_parser("add", help="enqueue trusted foreground argv; never starts execution")
    add.add_argument("--name", required=True)
    add.add_argument("--note", default="", help="optional plain-text human annotation (4096 Unicode code points); before --")
    add.add_argument("--engine", default="Command")
    add.add_argument("--progress-source", action="append", default=[], metavar="ROLE[@STAGE]=ABSOLUTE_PATH",
                     help="optional attempt-scoped scientific source declaration; repeat before --; files may not exist yet")
    add.add_argument("--suspension-backend", choices=("local-command-v1", "vasp-port-v1", "orca-local-v1"),
                     help="opt in to a qualified local suspension runtime handshake; backend registration may refuse")
    add.add_argument("--cwd", required=True, type=Path)
    add.add_argument("--cpus", type=int)
    add.add_argument("--memory-gib", type=float)
    add.add_argument("--mpi-ranks", type=int)
    add.add_argument("--threads-per-rank", type=int)
    add.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    add.add_argument("--on-failure", choices=("pause", "continue"), default="pause")
    add.add_argument("--hold", action="store_true")
    add.add_argument("--layout", type=Path, help="link and verify an existing CMW layout; does not bypass its runner")
    add.add_argument("--role", choices=("primary", "auxiliary"), default="primary")
    add.add_argument("--allow-auxiliary", action="store_true", help="primary scheduling consent; does not change the command")
    add.add_argument("--independent", action="store_true", help="assert no dependency on or unsafe writes to the primary")
    add.add_argument("--trust-resources", action="store_true", help="explicitly trust declared CPU/RAM bounds; not OS-enforced")
    add.add_argument("--write-scope", type=Path, help="declared write tree; defaults to resolved cwd")
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

    for name in ("suspend", "continue"):
        command = operations.add_parser(name, help=f"request {name} of a supported original managed attempt",
            description="Submit an asynchronous execution request to its existing worker. Accepted does not mean completed. "
                        "Use --preview for non-mutating readiness; show/status reports worker verification. "
                        "Memory, storage and admission commitments remain held; continuation is manual.")
        command.add_argument("job_id")
        command.add_argument("--preview", action="store_true", help="inspect readiness without creating or migrating Jobs state")
        command.add_argument("--reason", default="", help="optional literal operational reason; does not replace the job note")
        command.add_argument("--expected-revision", type=int, help="reject a request if suspension revision changed")
        command.add_argument("--expected-attempt-id", help="require this immutable attempt identity")
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=handle)

    observe = operations.add_parser("observe", help="read-only execution evidence for a managed job; never reconciles")
    observe.add_argument("job_id")
    observe.add_argument("--json", action="store_true")
    observe.set_defaults(handler=handle)

    reconcile = operations.add_parser("reconcile", help="preview conservative recovery; persistent apply is unavailable",
        description="Read-only reconciliation preview. Does not start a controller, signal, update state or release resources.")
    reconcile.add_argument("job_id")
    mode = reconcile.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="read-only preview (the default)")
    mode.add_argument("--apply", action="store_true", help="not implemented or qualified; always rejected")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(handler=handle)

    files = operations.add_parser("files", help="inspect declared job locations; opens folders only with --open")
    files.add_argument("job_id")
    files.add_argument("--json", action="store_true")
    files.add_argument("--open", action="store_true", help="explicitly request a desktop folder open")
    files.add_argument("--role", help="select one location role, for example output or working-directory")
    files.add_argument("--key", help="select the exact location key from files JSON")
    files.set_defaults(handler=handle)

    progress = operations.add_parser("progress", help="observe declared scientific progress; never controls execution")
    progress.add_argument("job_id")
    progress.add_argument("--json", action="store_true")
    progress.add_argument("--engine", help="explicit client-local adapter declaration for an opaque wrapper")
    progress.add_argument("--source", action="append", default=[], metavar="ROLE[@STAGE]=ABSOLUTE_PATH",
                          help="explicit client-local source, optionally stage-qualified; repeat in stage order")
    progress.add_argument("--history", action="store_true", help="read a bounded native history page")
    progress.add_argument("--snapshot", choices=("summary", "diagnostics", "json"),
                          help="capture an untruncated read-only handoff; --json selects JSON")
    progress.add_argument("--redact-paths", action="store_true", help="redact paths in a handoff snapshot")
    progress.add_argument("--limit", type=int, default=50, help="history entries per page (1–200)")
    progress.add_argument("--cursor", help="opaque history page cursor")
    progress.add_argument("--parent", help="history entry ID whose immediate children to show; * shows all levels")
    progress.add_argument("--stage", help="history stage filter")
    progress.add_argument("--segment", type=int, help="history run-segment filter")
    progress.add_argument("--history-polls", type=int, default=32, help="maximum bounded reconstruction slices (1–4096)")
    progress.set_defaults(handler=handle)

    annotate = operations.add_parser("annotate", help="replace or clear a managed job note; never controls execution",
        description="Atomic plain-text annotation replacement. Without --expected-revision, replaces the latest note. "
                    "Use the revision from show/status JSON to reject a concurrent edit. Maximum 4096 Unicode code points.")
    annotate.add_argument("job_id")
    edit = annotate.add_mutually_exclusive_group(required=True)
    edit.add_argument("--note", help="replacement note; CRLF and CR normalize to LF")
    edit.add_argument("--clear-note", action="store_true", help="explicitly clear the note")
    annotate.add_argument("--expected-revision", type=int, help="require this nonnegative note revision; conflicts exit 2")
    annotate.add_argument("--json", action="store_true")
    annotate.set_defaults(handler=handle)

    rename = operations.add_parser("rename-id", help="change an unstarted job's display prefix; preserves attempt identity")
    rename.add_argument("job_id")
    rename.add_argument("display_id")
    rename.add_argument("--json", action="store_true")
    rename.set_defaults(handler=handle)

    config = operations.add_parser("config", help="view/configure scheduler policy; never enables dispatch")
    config.add_argument("--mode", choices=("sequential", "bounded-sharing"))
    config.add_argument("--cpu-budget", type=int)
    config.add_argument("--memory-budget-gib", type=float)
    config.add_argument("--cpu-reserve", type=float)
    config.add_argument("--min-available-gib", type=float)
    config.add_argument("--json", action="store_true")
    config.set_defaults(handler=handle)
    sharing = operations.add_parser("sharing", help="declare a managed role/consent; never restarts execution")
    sharing.add_argument("job_id")
    sharing.add_argument("--role", choices=("primary", "auxiliary"))
    consent = sharing.add_mutually_exclusive_group()
    consent.add_argument("--allow-auxiliary", dest="allow_auxiliary", action="store_true")
    consent.add_argument("--revoke-auxiliary", dest="allow_auxiliary", action="store_false")
    sharing.set_defaults(allow_auxiliary=None)
    sharing.add_argument("--independent", action="store_true", default=None)
    sharing.add_argument("--trust-resources", action="store_true", default=None)
    sharing.add_argument("--write-scope", type=Path)
    sharing.add_argument("--json", action="store_true")
    sharing.set_defaults(handler=handle)
    reserve = operations.add_parser("reserve", help="declare identity-bound external coexistence; never adopts/signals it")
    reserve.add_argument("observation_id")
    reserve.add_argument("--cpus", type=int, required=True)
    reserve.add_argument("--memory-gib", type=float, required=True)
    reserve.add_argument("--write-scope", type=Path)
    reserve.add_argument("--json", action="store_true")
    reserve.set_defaults(handler=handle)
    unreserve = operations.add_parser("unreserve", help="remove external coexistence consent; running work is untouched")
    unreserve.add_argument("--json", action="store_true")
    unreserve.set_defaults(handler=handle)


def files_text(result):
    """Readable location evidence; no terminal markup or inferred ownership."""
    lines = [f"FILES {result['display_id']} — {result['status']}",
             f"State: {result['state_directory']}", f"Attempt: {result['attempt_id']}"]
    for location in result['locations']:
        stage = f" / {location['stage']}" if location.get('stage') else ""
        lines.append(f"{location['role']}{stage}: {location['accessibility']} — {location.get('path') or 'unknown'}")
        lines.append(f"  {location['kind']}; {location['origin']}; {location['qualification']}; key={location['key']}")
        if location.get('declared_path') and location.get('path') != location['declared_path']:
            lines.append(f"  Declared: {location['declared_path']}")
        if location.get('detail'):
            lines.append(f"  {location['detail']}")
    lines.extend(str(item) for item in result.get('diagnostics', ()))
    if 'open' in result:
        opened = result['open']
        lines.append(f"{opened['status']}: {opened['reason']}")
        if opened.get('path'):
            lines.append(opened['path'])
    return safe_text("\n".join(lines))


def inspect_files(store, args):
    from .locations import discover

    found = discover(store, args.job_id)
    result = found.to_dict()
    code = 0
    if args.open:
        from .folder_opener import open_location

        choices = list(found.locations)
        if args.key:
            choices = [item for item in choices if item.key == args.key]
        if args.role:
            choices = [item for item in choices if item.role == args.role]
        if not args.key and not args.role:
            choices = [item for item in choices if item.key == found.preferred_key]
        if len(choices) == 1:
            result['open'] = open_location(store, choices[0]).to_dict()
        else:
            result['open'] = {'status': 'AMBIGUOUS' if len(choices) > 1 or found.status == 'AMBIGUOUS' else 'LOCATION_UNKNOWN',
                              'path': None, 'reason': 'No unique selected location. Inspect the roles and full paths, then use --role or --key.'}
        code = 0 if result['open']['status'] == 'OPEN_REQUESTED' else 2
    elif args.key or args.role:
        # Filtering is a presentation choice, never a change to stored metadata.
        result['locations'] = [item for item in result['locations']
                               if (not args.key or item['key'] == args.key)
                               and (not args.role or item['role'] == args.role)]
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) if args.json else files_text(result))
    return code


def inspect_progress(store, args):
    from cmw.progress.formatting import history_text, progress_text
    from cmw.progress.service import ObservationService
    from .locations import _read_job
    from .progress import resolve_context
    from .source_declarations import parse_sources

    job, state_identity = _read_job(store, args.job_id)
    sources = parse_sources(args.source, args.engine or job['engine'])
    context = resolve_context(store, args.job_id, expected_attempt_id=job['attempt_id'],
                              sources=tuple(sources), engine=args.engine)
    if args.snapshot and args.history:
        raise JobsError('Snapshot and history are separate read-only views')
    if args.redact_paths and not args.snapshot:
        raise JobsError('--redact-paths requires --snapshot')
    if args.history:
        if not 1 <= args.limit <= 200 or not 1 <= args.history_polls <= 4096:
            raise JobsError("History --limit must be 1–200 and --history-polls must be 1–4096")
        if args.segment is not None and args.segment < 1:
            raise JobsError("History --segment must be positive")
    elif args.cursor or args.parent or args.stage or args.segment is not None:
        raise JobsError("History filters require --history")
    observer = ObservationService()
    try:
        if args.history:
            for _ in range(args.history_polls):
                snapshot = observer.history_page(context, cursor=args.cursor, limit=args.limit,
                    parent_id=args.parent, stage=args.stage, segment=args.segment)
                if snapshot.status != 'LOADING':
                    break
        else:
            snapshot = observer.poll(context)
    finally:
        observer.close()
    if args.snapshot:
        from .lifecycle import observe_execution
        from .snapshot import capture_snapshot, render_snapshot
        current, current_identity = _read_job(store, args.job_id)
        if current_identity != state_identity:
            raise JobsError('Jobs state identity changed while capturing snapshot; select it again')
        if current['attempt_id'] != job['attempt_id']:
            raise JobsError('Attempt identity changed while capturing snapshot; select it again')
        execution = observe_execution(store, current).to_dict()
        capture = capture_snapshot(current, progress=snapshot, execution=execution)
        print(render_snapshot(capture, mode='json' if args.json else args.snapshot,
                              redact_paths=args.redact_paths))
    else:
        print(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=True, allow_nan=False)
              if args.json else safe_text(history_text(snapshot) if args.history else progress_text(snapshot, detailed=True)))
    return 0


def handle(args):
    store = Store(args.state)
    operation = args.jobs_operation
    from .activity import project
    try:
        if operation in {"suspend", "continue"}:
            from .suspension import readiness, submit
            from .suspension_presentation import readiness_text, submission_text
            result = (readiness(store, args.job_id, operation) if args.preview else
                      submit(store, args.job_id, operation, reason=args.reason,
                             expected_attempt_id=args.expected_attempt_id,
                             expected_revision=args.expected_revision))
            print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) if args.json else
                  safe_text(readiness_text(result) if args.preview else submission_text(result)))
            return 0
        if operation == "observe":
            return inspect_lifecycle(store, args)
        if operation == "reconcile":
            return inspect_reconciliation(store, args)
        if operation == "files":
            return inspect_files(store, args)
        if operation == "progress":
            return inspect_progress(store, args)
        if operation == "watch":
            try:
                from .tui import JobsApp
            except ImportError as exc:
                raise JobsError('Console dependency missing; install with: pip install -e ".[jobs]"') from exc
            JobsApp(store).run()
            return 0
        if operation == "config":
            values = {"cpu_budget": args.cpu_budget, "memory_gib": args.memory_budget_gib,
                      "cpu_reserve": args.cpu_reserve, "min_available_gib": args.min_available_gib}
            current = store.snapshot()["scheduler"]
            result = (store.configure_sharing(args.mode or current["mode"], **values)
                      if args.mode is not None or any(value is not None for value in values.values()) else current)
        elif operation == "annotate":
            result = store.annotate(args.job_id, "" if args.clear_note else args.note, expected_revision=args.expected_revision)
        elif operation == "rename-id":
            result = store.rename_id(args.job_id, args.display_id)
        elif operation == "sharing":
            result = store.set_sharing(args.job_id, role=args.role, allow_auxiliary=args.allow_auxiliary,
                                      independent=args.independent, resource_contract="trusted-declared" if args.trust_resources else None,
                                      write_scope=args.write_scope)
        elif operation == "reserve":
            from .sharing import make_reservation
            state = project(store)
            observation = next((item for item in state['external_activity']['observations'] if item['id'] == args.observation_id), None)
            if observation is None:
                raise JobsError(f"External observation no longer observed: {args.observation_id}")
            reservation = make_reservation(observation, state['external_activity'], args.cpus, args.memory_gib, write_scope=args.write_scope)
            result = store.set_external_reservation(reservation)
        elif operation == "unreserve":
            result = store.set_external_reservation(None)
        elif operation == "add":
            from .source_declarations import parse_sources
            progress_sources = parse_sources(args.progress_source, args.engine)
            environment = {}
            for entry in args.env:
                if "=" not in entry:
                    raise JobsError("--env requires NAME=VALUE")
                key, value = entry.split("=", 1)
                environment[key] = value
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            result = store.add(argv=argv, cwd=args.cwd, name=args.name, engine=args.engine, note=args.note,
                cpus=args.cpus, memory_gib=args.memory_gib, mpi_ranks=args.mpi_ranks,
                threads_per_rank=args.threads_per_rank, env=environment, hold=args.hold,
                suspension_backend=args.suspension_backend, progress_sources=progress_sources,
                on_failure=args.on_failure, layout=args.layout, role=args.role, allow_auxiliary=args.allow_auxiliary,
                independent=args.independent, resource_contract="trusted-declared" if args.trust_resources else None, write_scope=args.write_scope)
        elif operation in {"start", "stop"}:
            from . import runtime
            getattr(runtime, operation)(store)
            result = project(store)
        elif operation in {"pause", "resume"}:
            store.dispatch(operation == "resume")
            result = project(store)
        elif operation in {"hold", "release", "move", "cancel"}:
            result = store.change(args.job_id, operation, position=getattr(args, "position", None), confirm=getattr(args, "yes", False))
        elif operation == "status":
            result = project(store)
        else:
            if str(args.job_id).startswith("E"):
                if operation != "show":
                    raise JobsError("External observations are read-only; external logs are not available")
                state = project(store)
                result = next((item for item in state["external_activity"]["observations"] if item["id"] == args.job_id), None)
                if result is None:
                    raise JobsError(f"External observation no longer observed: {args.job_id}")
                result = {**result, "scheduling_context": observation_sharing(state)}
                print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) if args.json else safe_text(external_detail(result)))
                return 0
            state = project(store) if operation == "show" else store.snapshot()
            candidates = [j for j in state["jobs"] if str(args.job_id) in {str(j["id"]), f"J{j['id']}", j["display_id"]}]
            if not candidates:
                raise JobsError(f"No such job: {args.job_id}")
            result = candidates[0]
            if operation == "show":
                from .lifecycle import observe_execution
                from .store import ACTIVE
                if result['status'] in ACTIVE:
                    observation = observe_execution(store, result, state=state).to_dict()
                    result = {**result, 'execution_observation': observation}
                    if result['status'] == 'Unknown':
                        result['ownership_observation'] = _unknown_ownership_observation(store, result, observation=observation)
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
