"""One transactional source of queue order, controls and execution evidence."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from uuid import uuid4

from cmw.core.execution_layout import resolve_recorded_layout

PENDING = {"Queue", "Hold"}
ACTIVE = {"Starting", "Run", "Cancelling", "Unknown"}
TERMINAL = {"Done", "Fail", "Cancelled"}


class JobsError(ValueError):
    """A rejected operation, with no implied successful mutation."""


def default_state() -> Path:
    if os.environ.get("CMW_JOBS_STATE"):
        return Path(os.environ["CMW_JOBS_STATE"]).expanduser().resolve()
    base = (Path.home() / "Library/Application Support" if sys.platform == "darwin"
            else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")))
    return base / "cmw/jobs"


def _positive(value, name, integer=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise JobsError(f"{name} must be positive and finite")
    if integer and int(value) != value:
        raise JobsError(f"{name} must be an integer")
    return int(value) if integer else value


def declared_cpu_demand(resources):
    """Declared MPI/thread metadata can contradict a smaller sharing budget."""
    return (resources.get('mpi_ranks') or 1) * (resources.get('threads_per_rank') or 1)


def scheduler_defaults():
    return {"mode": "sequential", "cpu_budget": None, "memory_gib": None,
            "cpu_reserve": None, "min_available_gib": None, "external_reservation": None}


def scheduling_defaults(cwd):
    return {"role": "primary", "allow_auxiliary": False, "independent": False,
            "resource_contract": None, "write_scope": cwd}


def _job_defaults(job):
    if 'cwd' in job:
        job.setdefault('scheduling', scheduling_defaults(job['cwd']))
        job.setdefault('sharing_anchor', None)
    return job


def _nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise JobsError(f"{name} must be nonnegative and finite")
    return value


def _scheduling(value, resources, cwd):
    result = dict(value)
    if not isinstance(result['role'], str) or result['role'] not in {'primary', 'auxiliary'}:
        raise JobsError('Scheduling role must be primary or auxiliary')
    if not isinstance(result['allow_auxiliary'], bool) or not isinstance(result['independent'], bool):
        raise JobsError('Sharing flags must be boolean')
    if result['resource_contract'] is not None and result['resource_contract'] != 'trusted-declared':
        raise JobsError('Resource contract must be trusted-declared')
    scope = result['write_scope']
    if not isinstance(scope, (str, Path)) or not str(scope) or '\0' in str(scope):
        raise JobsError('Write scope must be a nonempty path')
    location = Path(scope).expanduser()
    location = (location if location.is_absolute() else Path(cwd)/location).resolve()
    if not location.is_dir():
        raise JobsError('Write scope must be an existing directory')
    result['write_scope'] = str(location)
    if result['role'] == 'auxiliary':
        if result['allow_auxiliary']:
            raise JobsError('Only a primary may allow an auxiliary')
        if not result['independent'] or result['resource_contract'] != 'trusted-declared':
            raise JobsError('Auxiliary requires independence and an explicit trusted-declared resource contract')
        if resources.get('cpus') is None or resources.get('memory_gib') is None:
            raise JobsError('Auxiliary requires declared CPUs and memory')
        if declared_cpu_demand(resources) > resources['cpus']:
            raise JobsError('Declared MPI/thread demand exceeds Auxiliary CPU request')
    return result


class Store:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser().resolve() if root is not None else default_state()
        self.path = self.root / "queue.sqlite3"

    @contextmanager
    def transaction(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, job_id INTEGER, message TEXT)")
            con.execute("INSERT OR IGNORE INTO meta VALUES ('control', ?)", (json.dumps({"schema": 2, "dispatch": False, "stop": False, "reason": "Not started", "heartbeat": None, "owner": None, "scheduler": scheduler_defaults()}),))
            raw_control = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            if type(raw_control.get('schema')) is not int or raw_control['schema'] not in {1, 2}:
                raise JobsError("Unsupported Jobs state schema")
            if raw_control['schema'] == 1:
                # Old supervisors reject schema 2 on their next receipt/finalize
                # transaction. Keep their state readable until they are done.
                from .ownership import lock_held, owner_alive
                if (any(job['status'] in ACTIVE for job in self.rows(con)) or
                        lock_held(self.root/'controller.lock') or owner_alive(raw_control.get('owner'))):
                    raise JobsError('Legacy Jobs runtime still active or unresolved; let legacy attempts finish '
                                    'and stop the legacy controller with its original installation before migration')
                control = self.control(con)
                self.set_control(con, control)
                for job in self.rows(con):
                    self.save(con, job)
            yield con
            con.commit()
        except sqlite3.Error as exc:
            con.rollback()
            raise JobsError(f"Queue database unavailable: {exc}") from exc
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def control(con):
        control = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
        if type(control.get('schema')) is not int or control['schema'] not in {1, 2}:
            raise JobsError('Unsupported Jobs state schema')
        control['schema'] = 2
        control['scheduler'] = {**scheduler_defaults(), **control.get('scheduler', {})}
        return control

    @staticmethod
    def set_control(con, control):
        con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))

    @staticmethod
    def rows(con):
        return [_job_defaults(json.loads(row[0])) for row in con.execute("SELECT data FROM jobs ORDER BY id")]

    @staticmethod
    def get(con, job_id):
        try:
            number = int(str(job_id).removeprefix("J").split(".")[0])
        except ValueError as exc:
            raise JobsError(f"Invalid job ID: {job_id}") from exc
        row = con.execute("SELECT data FROM jobs WHERE id=?", (number,)).fetchone()
        if row is None:
            raise JobsError(f"No such job: {job_id}")
        job = _job_defaults(json.loads(row[0]))
        if "." in str(job_id) and str(job_id) != job["display_id"]:
            raise JobsError("Attempt identity differs")
        return job

    @staticmethod
    def save(con, job):
        con.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))

    @staticmethod
    def event(con, job_id, message):
        con.execute("INSERT INTO events(time,job_id,message) VALUES (?,?,?)", (time.time(), job_id, message))

    @classmethod
    def normalize(cls, con):
        for index, job in enumerate(sorted((j for j in cls.rows(con) if j["status"] in PENDING), key=lambda j: j["order"]), 1):
            job["order"] = index
            cls.save(con, job)

    def add(self, *, argv, cwd, name, engine="Command", cpus=None, memory_gib=None,
            mpi_ranks=None, threads_per_rank=None, env=None, hold=False,
            on_failure="pause", layout=None, role="primary", allow_auxiliary=False,
            independent=False, resource_contract=None, write_scope=None):
        argv = list(argv)
        if not argv or any(not isinstance(a, str) or "\0" in a for a in argv) or not argv[0]:
            raise JobsError("An explicit nonempty argv without NUL bytes is required")
        # Shell scripts must propagate command/pipeline failures, including a final echo.
        if Path(argv[0]).name in {"bash", "sh", "zsh"}:
            options = ["-e"] + (["-o", "pipefail"] if Path(argv[0]).name != "sh" else [])
            argv = [argv[0], *options, *argv[1:]]
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise JobsError(f"Working directory does not exist: {directory}")
        if not str(name).strip() or not str(engine).strip():
            raise JobsError("Name and engine are required")
        if on_failure not in {"pause", "continue"}:
            raise JobsError("on_failure must be pause or continue")
        environment = dict(env or {})
        for key, value in environment.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str) or "\0" in value:
                raise JobsError("Environment overrides require NAME=VALUE without NUL bytes")
        reference = None
        if layout:
            location = Path(layout).expanduser().resolve(strict=True)
            recorded = resolve_recorded_layout(json.loads(location.read_text()), metadata_path=location)
            if recorded.working_directory != directory:
                raise JobsError("Recorded layout working directory differs from --cwd")
            reference = {"path": str(location), "target_id": recorded.target_identifier,
                         "attempt_id": recorded.attempt_identifier}
        resources = {"cpus": _positive(cpus, "CPUs", True), "memory_gib": _positive(memory_gib, "Memory"),
                     "mpi_ranks": _positive(mpi_ranks, "MPI ranks", True),
                     "threads_per_rank": _positive(threads_per_rank, "Threads/rank", True)}
        scheduling = _scheduling(dict(role=role, allow_auxiliary=allow_auxiliary,
            independent=independent, resource_contract=resource_contract,
            write_scope=str(directory) if write_scope is None else write_scope), resources, directory)
        with self.transaction() as con:
            number = con.execute("INSERT INTO jobs(data) VALUES ('{}')").lastrowid
            pending = [j for j in self.rows(con) if j.get("status") in PENDING]
            job = {"id": number, "display_id": f"J{number}.1", "attempt_id": uuid4().hex,
                   "name": str(name), "engine": str(engine), "argv": argv, "cwd": str(directory),
                   "env": environment, "resources": resources, "layout": reference,
                   "status": "Hold" if hold else "Queue", "order": len(pending) + 1,
                   "on_failure": on_failure, "enqueued_at": time.time(), "started_at": None,
                   "finished_at": None, "exit_code": None, "signal": None, "reason": "",
                   "worker": None, "group": None, "claim": None, "claimed_at": None,
                   "cancel_requested": False, "scheduling": scheduling, "sharing_anchor": None}
            job["logs"] = {stream: str(self.root / "attempts" / job["attempt_id"] / f"{stream}.log") for stream in ("stdout", "stderr")}
            self.save(con, job)
            self.event(con, number, f"Enqueued at order {job['order']}")
        return job

    def configure_sharing(self, mode, *, cpu_budget=None, memory_gib=None,
                          cpu_reserve=None, min_available_gib=None):
        if not isinstance(mode, str) or mode not in {'sequential', 'bounded-sharing'}:
            raise JobsError('Scheduling mode must be sequential or bounded-sharing')
        updates = dict(cpu_budget=cpu_budget, memory_gib=memory_gib,
                       cpu_reserve=cpu_reserve, min_available_gib=min_available_gib)
        with self.transaction() as con:
            control = self.control(con)
            scheduler = dict(control['scheduler'])
            for key, value in updates.items():
                if value is not None:
                    scheduler[key] = (_positive(value, key, key == 'cpu_budget') if key in {'cpu_budget', 'memory_gib'}
                                      else _nonnegative(value, key))
            if mode == 'bounded-sharing' and any(scheduler[key] is None for key in updates):
                raise JobsError('First Bounded Sharing activation requires all four explicit resource policy values')
            if any(value is not None for value in updates.values()) or mode == 'bounded-sharing':
                import psutil
                try:
                    logical = psutil.cpu_count(logical=True)
                    physical_gib = psutil.virtual_memory().total / (1024 ** 3)
                except (psutil.Error, OSError) as exc:
                    raise JobsError('Physical host resources unavailable; cannot validate sharing policy') from exc
                if not logical or physical_gib <= 0:
                    raise JobsError('Physical host resources unavailable; cannot validate sharing policy')
                if (scheduler['cpu_budget'] is not None and scheduler['cpu_budget'] > logical or
                        scheduler['cpu_reserve'] is not None and scheduler['cpu_reserve'] >= logical):
                    raise JobsError('CPU policy exceeds observable logical CPU capacity')
                if (scheduler['memory_gib'] is not None and scheduler['memory_gib'] > physical_gib or
                        scheduler['min_available_gib'] is not None and scheduler['min_available_gib'] >= physical_gib):
                    raise JobsError('Memory policy exceeds observable physical RAM')
            scheduler['mode'] = mode
            control['scheduler'] = scheduler
            self.set_control(con, control)
            self.event(con, None, 'Scheduling policy configured: ' + mode)
        return scheduler

    def set_sharing(self, job_id, *, role=None, allow_auxiliary=None, independent=None,
                    resource_contract=None, write_scope=None):
        updates = {k: v for k, v in dict(role=role, allow_auxiliary=allow_auxiliary,
            independent=independent, resource_contract=resource_contract, write_scope=write_scope).items() if v is not None}
        with self.transaction() as con:
            job = self.get(con, job_id)
            if job['status'] not in PENDING:
                if (job['status'] not in ACTIVE or job['scheduling']['role'] != 'primary' or
                        set(updates) != {'allow_auxiliary'}):
                    raise JobsError('Only allow_auxiliary metadata may change on an active primary; other sharing edits require a pending job')
            job['scheduling'] = _scheduling({**job['scheduling'], **updates}, job['resources'], job['cwd'])
            self.save(con, job)
            self.event(con, job['id'], 'Sharing declaration updated')
        return job

    def set_external_reservation(self, reservation):
        if reservation is not None:
            keys = {'observation_id', 'fingerprint', 'cpu_budget', 'memory_gib', 'write_scope',
                    'created_at', 'source', 'allow_auxiliary'}
            if not isinstance(reservation, dict) or set(reservation) != keys:
                raise JobsError('External reservation has an invalid shape')
            if (not isinstance(reservation['observation_id'], str) or not re.fullmatch(r'E[0-9a-f]+', reservation['observation_id']) or
                    not isinstance(reservation['fingerprint'], str) or not re.fullmatch(r'[0-9a-f]{64}', reservation['fingerprint']) or
                    reservation['source'] != 'user supplied' or reservation['allow_auxiliary'] is not True):
                raise JobsError('External reservation identity/source is invalid')
            reservation = dict(reservation)
            if reservation['cpu_budget'] is None or reservation['memory_gib'] is None:
                raise JobsError('External reservation requires explicit CPU and memory budgets')
            reservation['cpu_budget'] = _positive(reservation['cpu_budget'], 'External CPUs', True)
            reservation['memory_gib'] = _positive(reservation['memory_gib'], 'External memory')
            _nonnegative(reservation['created_at'], 'Reservation timestamp')
            scope = reservation['write_scope']
            if (not isinstance(scope, str) or '\0' in scope or not Path(scope).is_absolute() or
                    not Path(scope).is_dir() or str(Path(scope).resolve()) != scope):
                raise JobsError('External reservation requires a resolved existing write directory')
        with self.transaction() as con:
            control = self.control(con)
            control['scheduler']['external_reservation'] = reservation
            self.set_control(con, control)
            self.event(con, None, 'External reservation revoked' if reservation is None else 'External reservation declared')
        return control['scheduler']

    def dispatch(self, enabled):
        with self.transaction() as con:
            control = self.control(con)
            if enabled and any(j["status"] in {"Unknown", "Cancelling"} for j in self.rows(con)):
                raise JobsError("Needs attention or cancellation is unresolved; dispatch remains paused")
            control.update(dispatch=bool(enabled), reason="" if enabled else "User pause")
            self.set_control(con, control)
            self.event(con, None, "Dispatch resumed" if enabled else "Dispatch paused")

    def change(self, job_id, action, *, position=None, confirm=False, expected=None):
        with self.transaction() as con:
            job = self.get(con, job_id)
            if expected is not None and job["status"] != expected:
                raise JobsError("Job changed since selection; refresh and try again")
            status = job["status"]
            if action in {"hold", "release", "move"} and status not in PENDING:
                raise JobsError("Only pending jobs can be held, released or reordered")
            if action == "hold":
                job["status"] = "Hold"
            elif action == "release":
                job["status"] = "Queue"
            elif action == "move":
                pending = sorted((j for j in self.rows(con) if j["status"] in PENDING), key=lambda j: j["order"])
                if position is None or not 1 <= position <= len(pending):
                    raise JobsError(f"Order must be between 1 and {len(pending)}")
                pending = [j for j in pending if j["id"] != job["id"]]
                pending.insert(position - 1, job)
                for index, item in enumerate(pending, 1):
                    item["order"] = index
                    self.save(con, item)
                job["order"] = position
            elif action == "cancel":
                if status in PENDING:
                    job.update(status="Cancelled", finished_at=time.time(), order=None, reason="Cancelled before execution")
                elif status in {"Starting", "Run"}:
                    if not confirm:
                        raise JobsError("Running cancellation requires --yes; it terminates the managed process group")
                    job.update(status="Cancelling", cancel_requested=True, reason="Waiting for confirmed termination")
                    control = self.control(con)
                    control.update(dispatch=False, reason="Cancellation requested; resume manually after termination")
                    self.set_control(con, control)
                else:
                    raise JobsError(f"Cannot cancel {status}; no unverified process will be signalled")
            else:
                raise JobsError(f"Unknown operation: {action}")
            self.save(con, job)
            self.normalize(con)
            self.event(con, job["id"], f"{action.capitalize()} requested")
        return job

    def snapshot(self, *, now=None):
        now = time.time() if now is None else now
        empty = {"schema": 2, "scheduler": scheduler_defaults(), "dispatch": False, "stop": False, "owner": None, "heartbeat": None, "reason": "No queue; use cmw jobs add, then start"}
        if not self.path.exists():
            control, jobs, events = empty, [], []
        else:
            # Read-only snapshots never create a database or acquire dispatch ownership.
            con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                con.execute("BEGIN")
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables:
                    # A rejected first mutation can roll back CREATE TABLE while
                    # leaving the new empty SQLite file. No existing data is reset.
                    control, jobs, events = empty, [], []
                else:
                    control, jobs = self.control(con), self.rows(con)
                    events = [dict(zip(("id", "time", "job_id", "message"), row)) for row in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30")][::-1]
            except sqlite3.Error as exc:
                raise JobsError(f"Queue database unavailable: {exc}") from exc
            finally:
                con.close()
        from .ownership import owner_alive, lock_held
        online = bool(control["owner"] and owner_alive(control["owner"]) and lock_held(self.root / "controller.lock"))
        age = None if control["heartbeat"] is None else max(0, now - control["heartbeat"])
        stale = bool(online and (age is None or age > 5))
        current = [j for j in jobs if j["status"] in ACTIVE]
        pending = sorted((j for j in jobs if j["status"] in PENDING), key=lambda j: j["order"])
        for job in jobs:
            job["elapsed"] = None if job["started_at"] is None else max(0, (job["finished_at"] or now) - job["started_at"])
            if job["status"] in PENDING:
                if job["status"] == "Hold":
                    job["reason"] = "User hold"
                elif current:
                    job["reason"] = "Active: " + ", ".join(f"{j['display_id']} ({j['status']})" for j in current)
                elif not online:
                    job["reason"] = "Controller offline; use start"
                elif stale:
                    job["reason"] = "Controller stale; needs attention"
                elif not control["dispatch"]:
                    job["reason"] = control["reason"] or "Dispatch paused"
                elif job["id"] != pending[0]["id"]:
                    job["reason"] = f"Queue order; behind {pending[0]['display_id']}"
                elif not job["reason"]:
                    job["reason"] = "Next in queue"
            job["check"] = None
        return {"schema": 2, "state_directory": str(self.root), "controller_log": str(self.root / "controller.log"),
                "controller": {**control, "online": online, "stale": stale, "age_seconds": age},
                "mode": "Bounded Sharing" if control["scheduler"]["mode"] == "bounded-sharing" else "Sequential",
                "scheduler": control["scheduler"], "observed_at": now, "jobs": jobs, "events": events}
