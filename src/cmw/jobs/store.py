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
            con.execute("INSERT OR IGNORE INTO meta VALUES ('control', ?)", (json.dumps({"schema": 1, "dispatch": False, "stop": False, "reason": "Not started", "heartbeat": None, "owner": None}),))
            if self.control(con)["schema"] != 1:
                raise JobsError("Unsupported Jobs state schema")
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
        return json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])

    @staticmethod
    def set_control(con, control):
        con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))

    @staticmethod
    def rows(con):
        return [json.loads(row[0]) for row in con.execute("SELECT data FROM jobs ORDER BY id")]

    @staticmethod
    def get(con, job_id):
        try:
            number = int(str(job_id).removeprefix("J").split(".")[0])
        except ValueError as exc:
            raise JobsError(f"Invalid job ID: {job_id}") from exc
        row = con.execute("SELECT data FROM jobs WHERE id=?", (number,)).fetchone()
        if row is None:
            raise JobsError(f"No such job: {job_id}")
        job = json.loads(row[0])
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
            on_failure="pause", layout=None):
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
                   "cancel_requested": False}
            job["logs"] = {stream: str(self.root / "attempts" / job["attempt_id"] / f"{stream}.log") for stream in ("stdout", "stderr")}
            self.save(con, job)
            self.event(con, number, f"Enqueued at order {job['order']}")
        return job

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
        empty = {"schema": 1, "dispatch": False, "stop": False, "owner": None, "heartbeat": None, "reason": "No queue; use cmw jobs add, then start"}
        if not self.path.exists():
            control, jobs, events = empty, [], []
        else:
            # Read-only snapshots never create a database or acquire dispatch ownership.
            con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                con.execute("BEGIN")
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
        current = next((j for j in jobs if j["status"] in ACTIVE), None)
        pending = sorted((j for j in jobs if j["status"] in PENDING), key=lambda j: j["order"])
        for job in jobs:
            job["elapsed"] = None if job["started_at"] is None else max(0, (job["finished_at"] or now) - job["started_at"])
            if job["status"] in PENDING:
                if job["status"] == "Hold":
                    job["reason"] = "User hold"
                elif current:
                    job["reason"] = f"Waiting for {current['display_id']} ({current['status']})"
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
        return {"schema": 1, "state_directory": str(self.root), "controller_log": str(self.root / "controller.log"),
                "controller": {**control, "online": online, "stale": stale, "age_seconds": age},
                "mode": "Sequential", "observed_at": now, "jobs": jobs, "events": events}
