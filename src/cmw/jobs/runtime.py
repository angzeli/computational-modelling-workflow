"""Detached local lifecycle and conservative reconciliation.

The packaged shell owns payload launch, descriptors, traps and exit-code capture.
This module provides the necessary POSIX session bootstrap, transactional launch
admission, ownership decisions and bounded process-group supervision.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from uuid import uuid4

from cmw.core.provenance import atomic_write_json
from .ownership import exclusive, group_exists, group_members, identity, lock_held, owner_alive, signal_session
from .store import ACTIVE, PENDING, JobsError, Store
from . import activity, sharing

INTERVAL = 0.15
CANCEL_GRACE = 2.0
_CHILDREN = []
_ADMISSION_SAMPLERS = {}


def reap_detached():
    _CHILDREN[:] = [process for process in _CHILDREN if process.poll() is None]


def runtime_environment():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(str(Path(p).resolve()) for p in sys.path if p))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def detached(arguments, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    reap_detached()
    with log.open("ab", buffering=0) as output:
        process = subprocess.Popen([sys.executable, "-m", "cmw.jobs.runtime", *arguments],
                                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                start_new_session=True, close_fds=True, env=runtime_environment(), cwd="/")
    _CHILDREN.append(process)
    return process


def start(store):
    # Serialize client start requests separately from the long-lived owner lock.
    with exclusive(store.root / "start.lock"):
        state = store.snapshot()
        if state["controller"]["online"]:
            raise JobsError("Controller already online; use resume to enable dispatch")
        if lock_held(store.root / "controller.lock"):
            raise JobsError("Controller ownership is uncertain; inspect controller.log")
        with store.transaction() as con:
            control = store.control(con)
            blocked = any(j["status"] in {"Unknown", "Cancelling"} for j in store.rows(con))
            control.update(stop=False, dispatch=not blocked, reason="Needs attention" if blocked else "Starting controller")
            store.set_control(con, control)
        process = detached(["controller", str(store.root)], store.root / "controller.log")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise JobsError("Controller failed to start; inspect controller.log")
            if store.snapshot()["controller"]["online"]:
                return
            time.sleep(0.05)
        raise JobsError("Controller startup not confirmed; inspect status before retrying")


def stop(store):
    with store.transaction() as con:
        control = store.control(con)
        control.update(stop=True, dispatch=False, reason="Controller stop requested; active execution continues")
        store.set_control(con, control)
        store.event(con, None, control["reason"])


def mark_unknown(store, con, job, reason):
    job.update(status="Unknown", reason=reason)
    store.save(con, job)
    control = store.control(con)
    control.update(dispatch=False, reason=f"Needs attention: {job['display_id']}: {reason}")
    store.set_control(con, control)
    store.event(con, job["id"], reason)


def reconcile(store, con, job):
    if job["status"] == "Unknown":
        return
    attempt = store.root / "attempts" / job["attempt_id"]
    if job["worker"] and owner_alive(job["worker"]) and lock_held(attempt / "worker.lock"):
        return
    # Starting may be between intent commit and worker registration. A grace
    # period blocks the slot but never authorizes relaunching this attempt.
    if job["worker"] is None and time.time() - job["claimed_at"] < 5:
        return
    mark_unknown(store, con, job, "Worker ownership/completion cannot be verified; automatic progression stopped")


def launch_blocker(job):
    if not Path(job["cwd"]).is_dir():
        return "Working directory no longer exists"
    command = job["argv"][0]
    path = {**os.environ, **job["env"]}.get("PATH", os.defpath)
    if "/" in command:
        executable = Path(command)
        if not executable.is_absolute():
            executable = Path(job["cwd"]) / executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return f"Executable is missing or not executable: {command}"
    else:
        # The detached controller runs in /; PATH lookup must match payload cwd.
        search_path = os.pathsep.join(
            str(Path(entry) if Path(entry).is_absolute() else Path(job["cwd"]) / entry)
            for entry in path.split(os.pathsep)
        )
        if shutil.which(command, path=search_path) is None:
            return f"Executable not found: {command}"
    return ""


def record_guard(store, con, guard):
    control = store.control(con)
    previous = control.get('external_guard', {})
    control['external_guard'] = guard
    store.set_control(con, control)
    if previous.get('state') != guard['state'] or previous.get('reason') != guard['reason']:
        store.event(con, None, 'External guard: ' + guard['state'] + ': ' + guard['reason'])
        # Bound guard transition history without pruning managed lifecycle events.
        con.execute("DELETE FROM events WHERE job_id IS NULL AND message LIKE 'External guard:%' "
                    "AND id NOT IN (SELECT id FROM events WHERE job_id IS NULL AND message LIKE 'External guard:%' ORDER BY id DESC LIMIT 100)")


def dispatch_authority(store, control):
    try:
        return (control['dispatch'] and not control['stop'] and owner_alive(control['owner'])
                and lock_held(store.root / 'controller.lock', strict=True))
    except OSError:
        return False


def tick(store):
    launch = None
    # OS collection is outside the write transaction. All controls/order are
    # read again under the existing claim transaction after the fresh scan.
    snapshot = store.snapshot()
    if snapshot.get('scheduler', {}).get('mode') == 'bounded-sharing':
        sampler = _ADMISSION_SAMPLERS.get(str(store.root))
        if sampler is None:
            sampler = _ADMISSION_SAMPLERS[str(store.root)] = sharing.AdmissionSampler()
        guard, evidence = sampler.collect(store, source='controller')
    else:
        guard = activity.DEFAULT_OBSERVER.scan(store, source='controller')
        evidence = None
    with store.transaction() as con:
        control = store.control(con)
        record_guard(store, con, guard)
        control = store.control(con)
        control["heartbeat"] = time.time()
        store.set_control(con, control)
        active = [j for j in store.rows(con) if j["status"] in ACTIVE]
        for job in active:
            reconcile(store, con, job)
        control = store.control(con)
        if control["stop"]:
            return False
        decision_state = {'jobs': store.rows(con), 'scheduler': control.get('scheduler', {}),
                          'controller': {**control, 'online': dispatch_authority(store, control), 'stale': False}}
        decision = sharing.evaluate(decision_state, guard, evidence)
        if not decision['permitted']:
            # Preflight reasons remain persistent; changing telemetry is only a
            # client/controller observation and never per-second queue history.
            if decision['candidate_id'] is not None:
                blocked = store.get(con, decision['candidate_id'])
                blocker = launch_blocker(blocked)
                if blocker and blocked['reason'] != blocker:
                    blocked['reason'] = blocker
                    store.save(con, blocked)
                    store.event(con, blocked['id'], blocker)
            return True
        job = store.get(con, decision['candidate_id'])
        # A refused supervisor returns the row to Queue before releasing its
        # attempt lock. Never reclaim it in that short cleanup interval.
        if lock_held(store.root / 'attempts' / job['attempt_id'] / 'worker.lock'):
            return True
        blocker = launch_blocker(job)
        if blocker:
            if job["reason"] != blocker:
                job["reason"] = blocker
                store.save(con, job)
                store.event(con, job["id"], blocker)
            return True
        job.update(admission_order=job["order"], sharing_anchor=decision["anchor_primary"],
                   status="Starting", order=None, claim=uuid4().hex,
                   claimed_at=time.time(), reason="Launch intent persisted")
        store.save(con, job)
        store.normalize(con)
        store.event(con, job["id"], "Launch intent acquired")
        launch = job
    if launch:
        try:
            detached(["worker", str(store.root), str(launch["id"]), launch["claim"]], store.root / "controller.log")
        except OSError as exc:
            # Popen failed before a child could exec. No attempt is relaunched.
            finish(store, launch["id"], launch["claim"], "Fail", None, f"Worker launch failed: {exc}")
    return True


def controller(store):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with exclusive(store.root / "controller.lock"):
        with store.transaction() as con:
            control = store.control(con)
            control.update(owner=identity(), heartbeat=time.time())
            store.set_control(con, control)
            store.event(con, None, "Controller online")
        print("CMW Jobs controller online", flush=True)
        try:
            while tick(store):
                # Reap only our finished guardians; they own their payloads.
                reap_detached()
                time.sleep(1.0)
        finally:
            with store.transaction() as con:
                control = store.control(con)
                control.update(owner=None, dispatch=False, reason="Controller offline")
                store.set_control(con, control)
                store.event(con, None, "Controller offline; active execution remains independent")
            print("CMW Jobs controller offline", flush=True)


def finish(store, job_id, claim, status, code, reason, *, termination_signal=None):
    with store.transaction() as con:
        job = store.get(con, job_id)
        if job["claim"] != claim or job["status"] not in ACTIVE:
            raise JobsError("Attempt no longer owns completion")
        if job["cancel_requested"] and status != "Unknown":
            status, reason = "Cancelled", "Managed process group termination confirmed"
        job.update(status=status, exit_code=code, signal=termination_signal,
                   finished_at=time.time(), reason=reason)
        store.save(con, job)
        control = store.control(con)
        if status == "Unknown" or status == "Cancelled" or (status == "Fail" and job["on_failure"] == "pause"):
            control.update(dispatch=False, reason=f"{job['display_id']}: {status}; manual attention/resume required")
            store.set_control(con, control)
        store.event(con, job["id"], f"{status}: {reason}")


def worker(store, job_id, claim):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with store.transaction() as con:
        initial = store.get(con, job_id)
    attempt = store.root / "attempts" / initial["attempt_id"]
    with exclusive(attempt / "worker.lock"):
        with store.transaction() as con:
            job = store.get(con, job_id)
            if job["claim"] != claim or job["status"] not in {"Starting", "Cancelling"} or job["worker"]:
                raise JobsError("Worker launch admission rejected")
            job["worker"] = identity()
            store.save(con, job)
        receipt = attempt / "payload-exit.json"
        env = runtime_environment()
        env.update(job["env"])
        try:
            # A pipe carries only GO/RELEASE, never computational output.
            leader = subprocess.Popen(["/bin/bash", str(Path(__file__).with_name("payload.sh")),
                sys.executable, runtime_environment()["PYTHONPATH"], str(receipt), job["logs"]["stdout"], job["logs"]["stderr"], *job["argv"]],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=job["cwd"], env=env, start_new_session=True, close_fds=True)
        except OSError as exc:
            finish(store, job_id, claim, "Fail", None, f"Launch failed: {exc}")
            return
        group = identity(leader.pid)
        try:
            before = store.snapshot()
            pending_ids = [(j['id'], j['order'], j['status']) for j in before['jobs'] if j['status'] in PENDING]
            if before.get('scheduler', {}).get('mode') == 'bounded-sharing':
                guard, evidence = sharing.AdmissionSampler().collect(store, source='supervisor-admission', wait=True)
            else:
                guard = activity.DEFAULT_OBSERVER.scan(store, before, source='supervisor-admission')
                evidence = None
            with store.transaction() as con:
                record_guard(store, con, guard)
                job = store.get(con, job_id)
                control = store.control(con)
                pending_now = [(j['id'], j['order'], j['status']) for j in store.rows(con) if j['status'] in PENDING]
                decision_state = {'jobs': store.rows(con), 'scheduler': control.get('scheduler', {}),
                                  'controller': {**control, 'online': dispatch_authority(store, control), 'stale': False}}
                decision = sharing.evaluate(decision_state, guard, evidence, candidate_id=job_id, final=True)
                admitted = (job['claim'] == claim and job['status'] == 'Starting' and
                            not job['cancel_requested'] and dispatch_authority(store, control) and
                            pending_ids == pending_now and decision['permitted'])
                if not admitted:
                    # Closing GO pipe ends only our not-yet-admitted shell. No
                    # payload or external process has been launched/signalled.
                    leader.stdin.close()
                    leader.wait(timeout=5)
                    if group_exists(leader.pid):
                        raise JobsError('Unadmitted payload helper has not exited')
                    if job['claim'] != claim or job['status'] not in {'Starting', 'Cancelling'}:
                        # Reconciliation/another identity remains authoritative;
                        # refusal must not erase Unknown or a changed claim.
                        return
                    if job['cancel_requested']:
                        job.update(status='Cancelled', finished_at=time.time(), reason='Cancelled before payload admission')
                    else:
                        pending = sorted((j for j in store.rows(con) if j['status'] in PENDING), key=lambda j: j['order'])
                        position = min(job.get('admission_order', 1), len(pending)+1)
                        # Auxiliary bypass never moves a refused candidate ahead
                        # of primaries that preceded it in the persistent queue.
                        for item in pending:
                            if item['order'] >= position:
                                item['order'] += 1
                                store.save(con, item)
                        job.update(status='Queue', order=position, reason=decision['reason'] if not decision['permitted'] else 'Admission controls/order changed')
                    job.update(worker=None, group=None, claim=None, claimed_at=None, sharing_anchor=None)
                    store.save(con, job)
                    store.normalize(con)
                    return
                job.update(group=group, started_at=time.time(), reason="")
                if not job["cancel_requested"]:
                    job["status"] = "Run"
                store.save(con, job)
                store.event(con, job["id"], "Execution started; group ownership persisted")
            leader.stdin.write(b"GO\n")
            leader.stdin.flush()
            cancel_at = None
            code = None
            session = job.get('env', {}).get('CMW_JOBS_OWN_SESSION') == '1'
            def remains():
                return group_exists(leader.pid) or (session and bool(group_members(leader.pid, session=True)))
            while True:
                with store.transaction() as con:
                    job = store.get(con, job_id)
                members = group_members(leader.pid, session=True) if session else group_members(leader.pid)
                if job["cancel_requested"]:
                    # The unreaped direct child pins this PID/group; birth identity
                    # additionally protects signalling from corrupted stale metadata.
                    if cancel_at is None:
                        if not owner_alive(group):
                            raise JobsError("Group leader lost before cancellation; no signal sent")
                        if session:
                            signal_session(group, signal.SIGTERM)
                        else:
                            os.killpg(leader.pid, signal.SIGTERM)
                        cancel_at = time.monotonic()
                    elif time.monotonic() - cancel_at > CANCEL_GRACE and members:
                        if not owner_alive(group):
                            raise JobsError("Group ownership lost during cancellation")
                        if session:
                            signal_session(group, signal.SIGKILL)
                        else:
                            os.killpg(leader.pid, signal.SIGKILL)
                        leader.wait(timeout=5)
                        # Reparented zombies are ended; live group members block.
                        deadline = time.monotonic() + 5
                        while remains() and time.monotonic() < deadline:
                            time.sleep(INTERVAL)
                        if remains():
                            raise JobsError("Process group termination remains uncertain")
                        finish(store, job_id, claim, "Cancelled", 137, "Termination confirmed", termination_signal=signal.SIGKILL)
                        return
                if receipt.exists():
                    evidence = json.loads(receipt.read_text())
                    code = int(evidence["exit_code"])
                    if members == [leader.pid] or set(members) == {leader.pid}:
                        leader.stdin.write(b"RELEASE\n")
                        leader.stdin.flush()
                        leader.wait(timeout=5)
                        if remains():
                            raise JobsError("Process group still contains live members after release")
                        if job['cancel_requested']:
                            finish(store, job_id, claim, 'Cancelled', code, 'Termination confirmed')
                        else:
                            finish(store, job_id, claim, "Done" if code == 0 else "Fail", code,
                                   "Execution contract completed; scientific checks not evaluated" if code == 0 else f"Exit code {code}")
                        return
                if not owner_alive(group):
                    raise JobsError("Group leader disappeared without a verified complete process tree")
                time.sleep(INTERVAL)
        except Exception as exc:
            with store.transaction() as con:
                mark_unknown(store, con, store.get(con, job_id), str(exc))
            # Do not terminate uncertain computations or fabricate completion.
        finally:
            if leader.stdin:
                leader.stdin.close()


def main():
    operation, *args = sys.argv[1:]
    if operation == "receipt":
        atomic_write_json(Path(args[0]), {"exit_code": int(args[1]), "recorded_at": time.time()})
        return
    store = Store(args[0])
    if operation == "controller":
        controller(store)
    elif operation == "worker":
        worker(store, int(args[1]), args[2])
    else:
        raise JobsError("Unknown internal lifecycle operation")


if __name__ == "__main__":
    main()
