"""Detached local lifecycle and conservative reconciliation.

The packaged shell owns payload launch, descriptors, traps and exit-code capture.
This module provides the necessary POSIX session bootstrap, transactional launch
admission, ownership decisions and bounded process-group supervision.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4

from cmw.core.provenance import atomic_write_json
from .ownership import exclusive, group_exists, group_members, identity, lock_held, owner_alive, signal_session
from .freshness import age_seconds, clock_stamp
from .guard_history import configure_recovery, record_guard
from .store import ACTIVE, PENDING, TERMINAL, JobsError, Store, private_opener
from . import activity, ownership_hold, sharing
from .ownership import (OwnershipStatus, OwnershipUnavailable, OwnershipContradiction,
                        is_unavailable, observe_owner)

INTERVAL = 0.15
CANCEL_GRACE = 2.0
CLOCK_STEP_THRESHOLD = 2.0
_UNSET_COMPLETION_TIME = object()
_CHILDREN = []
_ADMISSION_SAMPLERS = {}


def reap_detached():
    _CHILDREN[:] = [process for process in _CHILDREN if process.poll() is None]


def runtime_environment(overrides=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(str(Path(p).resolve()) for p in sys.path if p))
    if overrides:
        env.update(overrides)
    # A job can launch another CMW helper before its package initializer runs.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def detached(arguments, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    reap_detached()
    with open(log, "ab", buffering=0, opener=private_opener) as output:
        process = subprocess.Popen([sys.executable, "-m", "cmw.jobs.runtime", *arguments],
                                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                start_new_session=True, close_fds=True, env=runtime_environment(), cwd="/")
    _CHILDREN.append(process)
    return process


def start(store):
    store.ensure_private_root()
    # Serialize client start requests separately from the long-lived owner lock.
    with exclusive(store.root / "start.lock"):
        # A controller this client started can be a zombie until we poll it.
        reap_detached()
        state = store.snapshot()
        if state["controller"]["online"]:
            raise JobsError("Controller already online; use resume to enable dispatch")
        if ownership_hold.blocks_replacement(state["controller"]["owner"]) or lock_held(store.root / "controller.lock"):
            raise JobsError("Controller ownership is uncertain; inspect controller.log")
        with store.transaction() as con:
            _prepare_start(store, con)
        process = _launch_controller(store)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise JobsError("Controller failed to start; inspect controller.log")
            if store.snapshot()["controller"]["online"]:
                return
            time.sleep(0.05)
        raise JobsError("Controller startup not confirmed; inspect status before retrying")


def _prepare_start(store, con):
    control = store.control(con)
    if ownership_hold.blocks_replacement(control.get('owner')):
        raise JobsError('Existing controller ownership has not been disproved')
    blocked = any(j.get('ownership_holds') or j['status'] in {'Unknown', 'Cancelling'} for j in store.rows(con))
    control.update(stop=False, dispatch=not blocked, reason='Needs attention' if blocked else 'Starting controller')
    store.set_control(con, control)


def _launch_controller(store):
    return detached(['controller', str(store.root)], store.root / 'controller.log')


def _stop(store, con):
    control = store.control(con)
    control.update(stop=True, dispatch=False, reason='Controller stop requested; active execution state unchanged')
    store.set_control(con, control)
    store.event(con, None, control['reason'])


def stop(store):
    with store.transaction() as con:
        _stop(store, con)


def mark_unknown(store, con, job, reason):
    job.pop("ownership_holds", None)
    job.pop("ownership_resume_status", None)
    job.update(status="Unknown", reason=reason)
    store.save(con, job)
    control = store.control(con)
    control.update(dispatch=False, reason=f"Needs attention: {job['display_id']}: {reason}")
    store.set_control(con, control)
    store.event(con, job["id"], reason)


def reconcile(store, con, job):
    if job["status"] == "Unknown" and not job.get("ownership_holds"):
        return
    attempt = store.root / "attempts" / job["attempt_id"]
    if job["worker"]:
        result = observe_owner(job['worker'])
        if result.status == OwnershipStatus.UNVERIFIABLE:
            ownership_hold.unavailable(store, con, job, 'controller', f'worker.{result.deciding_check}', time.monotonic())
            return
        if result.status == OwnershipStatus.GONE:
            mark_unknown(store, con, job, f'Recorded worker is gone ({result.deciding_check}); completion unverified')
            return
        try:
            locked = lock_held(attempt / 'worker.lock', strict=True)
        except OSError:
            ownership_hold.unavailable(store, con, job, 'controller', 'worker.lock', time.monotonic())
            return
        if locked:
            ownership_hold.recovered(store, con, job, 'controller')
            return
    # Starting may be between intent commit and worker registration. A grace
    # period blocks the slot but never authorizes relaunching this attempt.
    if job["worker"] is None:
        claim_clock = job.get("claim_clock")
        current_clock = clock_stamp()
        claim_age = age_seconds(claim_clock, current_clock)
        if claim_age is not None and claim_age < 5:
            return
        if claim_age is None and job["status"] == "Starting":
            reason = "Workerless Starting: launch grace cannot be established"
            if claim_clock is None:
                reason += " from a legacy or previous-boot stamp (claim stamp missing)"
            elif (isinstance(claim_clock, dict) and claim_clock.get("boot_session_id")
                  and current_clock and claim_clock["boot_session_id"] != current_clock["boot_session_id"]):
                reason += " from a legacy or previous-boot stamp (boot session differs)"
            else:
                reason += " because the recorded and current monotonic clocks are incomparable"
            mark_unknown(store, con, job, reason)
            return
    mark_unknown(store, con, job, "Worker ownership/completion cannot be verified; automatic progression stopped")


def launch_blocker(job, *, final=False):
    if 'node_submission' in job:
        from cmw.nodes.submission import launch_blocker as node_launch_blocker
        try:
            blocker = node_launch_blocker(job, final=final)
        except OSError as exc:
            raise OwnershipUnavailable('launch.preflight', 'Node launch preflight temporarily unreadable') from exc
        except ValueError as exc:
            return f'Node launch preflight refused: {exc}'
        if blocker:
            return str(blocker)
    if job.get('molecular_submission'):
        from .molecular import launch_blocker as molecular_launch_blocker
        blocker = molecular_launch_blocker(job, final=final)
        if blocker:
            return blocker
    def mode(path):
        try:
            return path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return 0
    if not stat.S_ISDIR(mode(Path(job['cwd']))):
        return "Working directory no longer exists"
    command = job["argv"][0]
    path = {**os.environ, **job["env"]}.get("PATH", os.defpath)
    if "/" in command:
        executable = Path(command)
        if not executable.is_absolute():
            executable = Path(job["cwd"]) / executable
        if not stat.S_ISREG(mode(executable)) or not os.access(executable, os.X_OK):
            return f"Executable is missing or not executable: {command}"
    else:
        # The detached controller runs in /; PATH lookup must match payload cwd.
        search_path = os.pathsep.join(
            str(Path(entry) if Path(entry).is_absolute() else Path(job["cwd"]) / entry)
            for entry in path.split(os.pathsep)
        )
        if not any(stat.S_ISREG(mode(Path(entry)/command)) and os.access(Path(entry)/command, os.X_OK)
                   for entry in search_path.split(os.pathsep)):
            return f"Executable not found: {command}"
    return ""


def dispatch_authority(store, control):
    if not control['dispatch'] or control['stop']:
        return False
    observed = observe_owner(control['owner'])
    if observed.status == OwnershipStatus.UNVERIFIABLE:
        raise OwnershipUnavailable('controller.' + observed.deciding_check, 'Controller authority is temporarily unreadable')
    return observed.status == OwnershipStatus.VERIFIED and lock_held(store.root / 'controller.lock', strict=True)


def update_heartbeat(store, con, control):
    wall = time.time()
    current = clock_stamp()
    elapsed = age_seconds(control.get('heartbeat_clock'), current)
    previous = control.get('heartbeat')
    if elapsed is not None and isinstance(previous, (float, int)):
        step = wall - previous - elapsed
        if abs(step) > CLOCK_STEP_THRESHOLD:
            store.event(con, None, f'Wall clock stepped by {step:+.3f} s')
    control.update(heartbeat=wall, heartbeat_clock=current)


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
        update_heartbeat(store, con, control)
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
                   claimed_at=time.time(), claim_clock=clock_stamp(), reason="Launch intent persisted")
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
    store.ensure_private_root()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with exclusive(store.root / "controller.lock"):
        with store.transaction() as con:
            control = store.control(con)
            control.update(owner=identity(), heartbeat=time.time(), heartbeat_clock=clock_stamp(), runtime_protocol=3)
            configure_recovery(store, con, control)
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


def finish(store, job_id, claim, status, code, reason, *, termination_signal=None, wait=None,
           attempt_id=None, finished_at=_UNSET_COMPLETION_TIME, timing=None):
    """Publish established completion once, even if COMMIT's outcome is unreadable.

    The caller has already finished all process control. Every retry starts with
    readback under the write lock; it never repeats a signal, RELEASE or a
    terminal event. The original worker remains the sole recovery owner.
    """
    pause = wait or time.sleep
    finished_at = time.time() if finished_at is _UNSET_COMPLETION_TIME else finished_at
    delay = .25
    bound_attempt = attempt_id
    final_timing = deepcopy(timing)
    while True:
        try:
            with store.transaction(create=False, wait=pause) as con:
                job = store.get(con, job_id)
                if bound_attempt is None:
                    bound_attempt = job['attempt_id']
                if job['claim'] != claim or job['attempt_id'] != bound_attempt:
                    raise OwnershipContradiction('Attempt no longer owns completion')
                terminal_status = 'Cancelled' if job['cancel_requested'] and status != 'Unknown' else status
                terminal_reason = ('Managed process group termination confirmed'
                                   if terminal_status == 'Cancelled' else reason)
                if job['status'] in TERMINAL:
                    if (job['status'], job.get('exit_code'), job.get('signal')) == (
                            terminal_status, code, termination_signal):
                        return  # An earlier COMMIT succeeded; acknowledge, never replay.
                    raise OwnershipContradiction('Committed completion contradicts established evidence')
                if job['status'] not in ACTIVE:
                    raise OwnershipContradiction('Attempt no longer owns completion')
                if job['status'] == 'Unknown' and status != 'Unknown' and not job.get('ownership_resume_status'):
                    raise OwnershipContradiction('Contradictory Unknown state cannot be overwritten by completion')
                # Read everything before changing anything. A failed read rolls
                # this transaction back; an uncertain commit is resolved above.
                control = store.control(con)
                if final_timing is None and job.get('suspension'):
                    from .suspension import finalize_timing
                    frozen = deepcopy(job)
                    finalize_timing(frozen)
                    final_timing = frozen['suspension']
                job.update(status=terminal_status, exit_code=code, signal=termination_signal,
                           finished_at=finished_at, reason=terminal_reason)
                if final_timing is not None:
                    job['suspension'] = deepcopy(final_timing)
                job.pop('ownership_holds', None)
                job.pop('ownership_resume_status', None)
                store.save(con, job)
                if terminal_status in {'Unknown', 'Cancelled'} or (terminal_status == 'Fail' and job['on_failure'] == 'pause'):
                    control.update(dispatch=False, reason=f"{job['display_id']}: {terminal_status}; manual attention/resume required")
                    store.set_control(con, control)
                store.event(con, job['id'], f'{terminal_status}: {terminal_reason}')
            return
        except Exception as exc:
            if isinstance(exc, OwnershipContradiction):
                raise
            pause(delay)
            delay = min(delay * 2, ownership_hold.MAX_RETRY)


def _wait_for_group_end(pgid, *, session=False, wait=None):
    """Retain commitment until the kernel confirms absence after leader reaping."""
    delay = .25
    while True:
        try:
            if not group_exists(pgid) and not (session and group_members(pgid, session=True)):
                return
        except Exception as exc:
            if not is_unavailable(exc):
                raise
        # Presence and unreadability are both nonterminal observations. Neither
        # becomes a contradiction merely because five seconds have passed.
        (wait or time.sleep)(delay)
        delay = min(delay * 2, ownership_hold.MAX_RETRY)


def _receipt(path):
    """A missing receipt differs from an unreadable one, on every Python version."""
    try:
        with path.open('rb') as handle:
            payload = handle.read(4097)
    except FileNotFoundError:
        return None
    try:
        value = json.loads(payload)
        if len(payload) > 4096 or not isinstance(value, dict) or type(value.get('exit_code')) is not int:
            raise ValueError('invalid receipt')
    except (ValueError, UnicodeError) as exc:
        raise OwnershipContradiction('Payload receipt is malformed') from exc
    return value, hashlib.sha256(payload).hexdigest()


def _read_job(store, job_id):
    with store.transaction(create=False, wait=time.sleep) as con:
        return store.get(con, job_id)


def _commit_job(store, con, job, wait):
    """Resolve a COMMIT acknowledgement before an irreversible pipe operation."""
    delay = .25
    while True:
        try:
            con.commit()
            return
        except Exception as exc:
            if not is_unavailable(exc):
                raise
            if not con.in_transaction:
                observed = ownership_hold.retry_read(lambda: store.get(con, job['id']), wait=wait)
                if observed == job:
                    return
                raise OwnershipUnavailable('queue.commit', 'Queue transaction rolled back before delivery') from exc
            wait(delay)
            delay = min(delay * 2, ownership_hold.MAX_RETRY)


class _AdmissionRefusal:
    """Own a refused, never-started helper through closure and queue readback."""

    def __init__(self, store, job, authority, leader, *, attempt_id, blocker, reason,
                 coordinator=None):
        self.store, self.job_id, self.authority, self.leader = store, job['id'], authority, leader
        self.attempt_id, self.blocker, self.reason = attempt_id, blocker, reason
        self.coordinator = coordinator
        self.closed = False

    def _owns(self, job):
        return (job['attempt_id'], job['claim'], job['worker'], job['group']) == (
            self.attempt_id, self.authority.claim, self.authority.worker, self.authority.anchor)

    def prepare(self, con, job, check='unstarted helper exit'):
        # This operation is installed before committing the marker. No retry
        # after closing its pipe is allowed to ask the departed anchor for trust.
        if self._owns(job) and job['status'] in {'Starting', 'Cancelling'}:
            ownership_hold.unavailable(self.store, con, job, 'admission', check, time.monotonic())

    def _reserve(self):
        with self.store.transaction(create=False, wait=self.authority.queue_wait) as con:
            self.prepare(con, self.store.get(con, self.job_id))

    def report(self, check, error):
        def no_wait(delay):
            raise OwnershipUnavailable('admission.queue', 'Admission refusal reporting queue is busy')
        try:
            with self.store.transaction(create=False, wait=no_wait) as con:
                self.prepare(con, self.store.get(con, self.job_id), check)
        except Exception as reporting:
            if isinstance(reporting, OwnershipContradiction):
                raise
            self.authority._report_read('admission.' + check, reporting)

    def read(self, check, observation):
        return ownership_hold.retry_read(observation, wait=self.authority.queue_wait,
            on_unavailable=lambda error: self.report(check, error))

    def _publish(self):
        with self.store.transaction(create=False, wait=self.authority.queue_wait) as con:
            job = self.store.get(con, self.job_id)
            # A successful but unacknowledged commit already cleared this claim.
            # A newer claim or independent Unknown remains authoritative too.
            if not self._owns(job) or job['status'] not in {'Starting', 'Cancelling'}:
                return
            if job['cancel_requested']:
                job.update(status='Cancelled', finished_at=time.time(), reason='Cancelled before payload admission')
            else:
                pending = sorted((item for item in self.store.rows(con) if item['status'] in PENDING),
                                 key=lambda item: item['order'])
                position = min(job.get('admission_order', 1), len(pending) + 1)
                for item in pending:
                    if item['order'] >= position:
                        item['order'] += 1
                        self.store.save(con, item)
                job.update(status='Hold' if self.blocker else 'Queue', order=position,
                           reason=self.blocker or self.reason)
            job.update(worker=None, group=None, claim=None, claimed_at=None, claim_clock=None, sharing_anchor=None)
            job.pop('ownership_holds', None)
            job.pop('ownership_resume_status', None)
            self.store.save(con, job)
            self.store.normalize(con)

    def finish(self):
        # The original gate has exited before any wait or a new transaction.
        # Read back the refusal marker even if its first COMMIT was ambiguous.
        self.read('refusal publication', self._reserve)
        if not self.closed:
            self.closed = True
            self.leader.stdin.close()  # EOF ends only this unadmitted helper.

        def reap():
            try:
                return self.leader.wait(timeout=.25)
            except subprocess.TimeoutExpired as exc:
                raise OwnershipUnavailable('admission.exit', 'Unstarted helper exit pending') from exc
        self.read('unstarted helper exit', reap)

        def absent():
            if group_exists(self.leader.pid):
                raise OwnershipUnavailable('admission.absence', 'Unstarted helper absence pending')
        self.read('unstarted helper absence', absent)
        if self.coordinator:
            self.read('unstarted channel cleanup', self.coordinator.discard_before_go)
        self.read('refusal publication', self._publish)


class _Completion:
    """The original worker's in-memory finisher; it never adopts or replays control."""

    def __init__(self, store, job, authority, leader, *, coordinator=None, session=False):
        self.store, self.job, self.authority, self.leader = store, deepcopy(job), authority, leader
        self.coordinator, self.session = coordinator, session
        self.release_attempted = False
        self.receipt_evidence = None
        self.finished_at = None
        self.timing = None
        self.authority.completing = True

    def _pending(self, con, job, check):
        if (job['attempt_id'], job['claim'], job['worker'], job['group']) != (
                self.job['attempt_id'], self.job['claim'], self.job['worker'], self.job['group']):
            raise OwnershipContradiction('Completion attempt binding changed')
        if job['status'] in TERMINAL:
            return
        ownership_hold.unavailable(self.store, con, job, 'completion', check, time.monotonic())
        reason = f'Completion pending: {check}; original worker retains completion evidence'
        if job.get('reason') != reason:
            job['reason'] = reason
            self.store.save(con, job)

    def report(self, check):
        def no_wait(delay):
            raise OwnershipUnavailable('completion.queue', 'Completion reporting queue is busy')
        try:
            with self.store.transaction(create=False, wait=no_wait) as con:
                self._pending(con, self.store.get(con, self.job['id']), check)
        except Exception as exc:
            if isinstance(exc, OwnershipContradiction):
                raise
            # An unavailable reporting channel cannot discard the finisher.
            self.authority._report_read('completion.' + check, exc)

    def read(self, check, observation):
        return ownership_hold.retry_read(observation, wait=self.authority.queue_wait,
                                         on_unavailable=lambda exc: self.report(check))

    def _drained(self):
        if self.coordinator:
            return self.coordinator.ready_to_release()
        members = (group_members(self.leader.pid, session=True) if self.session
                   else group_members(self.leader.pid))
        return set(members) == {self.leader.pid}

    def freeze_timing(self):
        if getattr(self, '_timing_frozen', False):
            return
        frozen = deepcopy(self.job)
        unavailable = []
        if frozen.get('suspension'):
            from .suspension import finalize_timing
            from .suspension_protocol import clock_descriptor, now_clock
            try:
                clock = now_clock(), clock_descriptor()
            except Exception as exc:
                # A later successful clock read cannot reconstruct this earlier
                # boundary. Retain completion, but do not count retry time as work.
                value = frozen['suspension']
                value.update(accounting_uncertain=True, unsuspended_elapsed=None, verification_fresh=False,
                             reason='Completion clock unavailable at the established boundary; duration unverifiable')
                value.pop('clock_finished', None)
                unavailable.append('completion clock unavailable at the established boundary: ' + str(exc))
            else:
                finalize_timing(frozen, now=clock[0], descriptor=clock[1])
            self.timing = frozen['suspension']
        try:
            self.finished_at = time.time()
        except Exception as exc:
            self.finished_at = None
            unavailable.append('completion timestamp unavailable at the established boundary: ' + str(exc))
        self._timing_frozen = True
        for check in unavailable:
            self.report(check)

    def abandon_preparation(self):
        if self.release_attempted:
            raise OwnershipContradiction('A RELEASE attempt cannot return to live supervision')
        def clear():
            with self.store.transaction(create=False, wait=self.authority.queue_wait) as con:
                current = self.store.get(con, self.job['id'])
                if (current['attempt_id'], current['claim']) != (self.job['attempt_id'], self.job['claim']):
                    raise OwnershipContradiction('Completion attempt changed during preparation')
                ownership_hold.recovered(self.store, con, current, 'completion', verified=False)
                if current.get('reason', '').startswith('Completion pending:'):
                    current['reason'] = 'Completion preparation superseded; observing current intent'
                    self.store.save(con, current)
        ownership_hold.retry_read(clear, wait=self.authority.queue_wait)
        self.authority.completing = False

    def release(self, receipt, evidence):
        self.receipt_evidence = evidence
        delay = .25
        while not self.release_attempted:
            try:
                self.authority.wait()
                current = self.authority.read_domain(lambda: _receipt(receipt))
                if current is None:
                    raise OwnershipUnavailable('receipt', 'Previously observed receipt is unavailable')
                if current != evidence:
                    raise OwnershipContradiction('Payload receipt changed before RELEASE')
                if not self.authority.read_domain(self._drained):
                    raise OwnershipUnavailable('drainage', 'Awaiting complete drainage before RELEASE')
                generation = self.authority.generation
                self.freeze_timing()
                with _worker_intent(self.store, self.job, self.coordinator,
                                    self.authority.queue_wait, completion=True) as (con, latest):
                    if self.authority.generation != generation:
                        continue  # Gate acquisition waited; obtain fresh drainage/authority.
                    self._pending(con, latest, 'release and final absence')
                    # Retain the same exclusive database gate through the pipe
                    # write, as the existing GO admission path does.
                    if con.execute('PRAGMA locking_mode=EXCLUSIVE').fetchone()[0] != 'exclusive':
                        raise OwnershipContradiction('Cannot retain completion intent through RELEASE')
                    _commit_job(self.store, con, latest, self.authority.queue_wait)
                    if self.authority.generation != generation:
                        continue  # COMMIT waited too; the pending marker is idempotent.
                    self.release_attempted = True
                    self.leader.stdin.write(b'RELEASE\n')
                    self.leader.stdin.flush()
            except Exception as exc:
                if not is_unavailable(exc):
                    raise
                self.report('release outcome' if self.release_attempted else 'release preparation')
                self.authority.queue_wait(delay)
                delay = min(delay * 2, ownership_hold.MAX_RETRY)
        # After this point no path returns to signal delivery or RELEASE.
        def verify_receipt():
            current = _receipt(receipt)
            if current is None:
                raise OwnershipUnavailable('receipt', 'Completion receipt is temporarily unavailable')
            if current != evidence:
                raise OwnershipContradiction('Payload receipt changed after RELEASE')
        self.read('receipt verification', verify_receipt)

    def finish(self, status, code, reason, *, termination_signal=None):
        def reap():
            try:
                return self.leader.wait(timeout=.25)
            except subprocess.TimeoutExpired as exc:
                raise OwnershipUnavailable('anchor.exit', 'Awaiting original anchor exit') from exc
        returncode = self.read('anchor exit', reap)
        if self.release_attempted and returncode != 0:
            raise OwnershipContradiction('Protected anchor did not acknowledge normal RELEASE')
        self.freeze_timing()

        def absent():
            if self.coordinator:
                if not self.coordinator.verify_released():
                    raise OwnershipUnavailable('drainage', 'Original control domain is still draining')
            elif group_exists(self.leader.pid) or (self.session and group_members(self.leader.pid, session=True)):
                raise OwnershipUnavailable('drainage', 'Original group/session is still draining')
        self.read('final absence observation', absent)
        # Freeze at the first established completion observation, not a later
        # successful queue write. No live-anchor verification is needed here.
        self.freeze_timing()
        self.report('queue publication')
        def publication_wait(delay):
            self.report('queue publication')
            self.authority.queue_wait(delay)
        finish(self.store, self.job['id'], self.job['claim'], status, code, reason,
               termination_signal=termination_signal, wait=publication_wait,
               attempt_id=self.job['attempt_id'], finished_at=self.finished_at,
               timing=self.timing)


def _wait_unobserved_domain(store, job, authority, reason):
    """Retain the original pipe when live historical evidence was never captured.

    No later observation can manufacture that history. This driver only keeps
    the manual-review hold visible; it cannot signal, RELEASE or finalize.
    An explicitly changed/closed attempt belongs to the operator, not this loop.
    """
    delay = .25
    while True:
        try:
            with store.transaction(create=False, wait=authority.queue_wait) as con:
                current = store.get(con, job['id'])
                if (current['attempt_id'], current['claim'], current['worker'], current['group']) != (
                        job['attempt_id'], job['claim'], job['worker'], job['group']):
                    return
                if current['status'] in TERMINAL:
                    return
                ownership_hold.unavailable(store, con, current, 'completion', reason, time.monotonic())
                if current.get('reason') != reason:
                    current['reason'] = reason
                    store.save(con, current)
        except Exception as exc:
            authority._report_read('completion.domain_never_observed', exc)
        authority.queue_wait(delay)
        delay = min(delay * 2, ownership_hold.MAX_RETRY)


@contextmanager
def _worker_intent(store, expected, coordinator, wait, *, completion=False):
    """Serialize the final syscall/pipe write with queue mutations, never a probe."""
    from .suspension import RequestSuperseded
    if coordinator and not completion:
        terminating = expected.get('cancel_requested') or expected.get('termination_requested')
        with coordinator._intent(expected, check_deadline=not terminating, delivery=True):
            yield
        return
    gate = store.transaction if completion else store.delivery_gate
    with gate(wait=wait) as con:
        latest = store.get(con, expected['id'])
        if any(latest.get(key) != expected.get(key) for key in ('attempt_id', 'claim', 'worker', 'group')):
            raise OwnershipContradiction('Recorded worker or group authority changed; refusing control')
        holds = set(latest.get('ownership_holds', {})) - ({'completion'} if completion else set())
        if (holds or
                any(latest.get(key) != expected.get(key) for key in
                    ('status', 'cancel_requested', 'termination_requested'))):
            raise RequestSuperseded('Control request or termination intent changed')
        if coordinator and any((latest.get('suspension') or {}).get(key) !=
                               (expected.get('suspension') or {}).get(key) for key in ('revision', 'request')):
            raise RequestSuperseded('Control request or termination intent changed')
        if coordinator:
            from .suspension import _file_identity
            if _file_identity(store.path) != coordinator.context['state_identity']:
                raise OwnershipContradiction('Jobs state identity changed before completion')
        yield con, latest


def worker(store, job_id, claim):
    from .suspension import CompletionDomainUnobserved, RequestSuperseded
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    initial = ownership_hold.retry_read(lambda: _read_job(store, job_id))
    attempt = store.root / "attempts" / initial["attempt_id"]
    attempt.parent.mkdir(exist_ok=True, mode=0o700)
    with exclusive(attempt / "worker.lock"):
        worker_identity = ownership_hold.retry_read(identity)
        def register_worker():
            with store.transaction(create=False, wait=time.sleep) as con:
                job = store.get(con, job_id)
                if (job['claim'] != claim or job['attempt_id'] != initial['attempt_id']
                        or job['status'] not in {'Starting', 'Cancelling'}
                        or job['worker'] not in (None, worker_identity)):
                    raise OwnershipContradiction('Worker launch admission rejected')
                if job['worker'] is None:
                    job['worker'] = worker_identity
                    job['worker_protocol'] = 3
                    store.save(con, job)
                return job
        job = ownership_hold.retry_read(register_worker)
        receipt = attempt / "payload-exit.json"
        env = ownership_hold.retry_read(lambda: runtime_environment(job['env']))
        env.pop('CMW_SUSPENSION_CONTEXT', None)
        # Payloads may bind their own evidence to this exact attempt.
        env['CMW_JOBS_ATTEMPT_ID'] = job['attempt_id']
        coordinator = None
        if job.get('suspension'):
            env['CMW_SUSPENSION_CONTEXT'] = str(attempt/'suspension'/'context.json')
        import_path = ownership_hold.retry_read(lambda: runtime_environment()['PYTHONPATH'])
        try:
            # A pipe carries only GO/RELEASE, never computational output.
            leader = subprocess.Popen(["/bin/bash", str(Path(__file__).with_name("payload.sh")),
                sys.executable, import_path, str(receipt), job["logs"]["stdout"], job["logs"]["stderr"], *job["argv"]],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=job["cwd"], env=env, start_new_session=True, close_fds=True)
        except OSError as exc:
            finish(store, job_id, claim, "Fail", None, f"Launch failed: {exc}")
            return
        try:
            # Popen retains our unreaped direct child. An unavailable initial
            # start token delays GO; it never permits adopting another PID.
            def capture_anchor():
                try:
                    return identity(leader.pid)
                except Exception as exc:
                    if ownership_hold.retry_read(leader.poll) is not None:
                        raise OwnershipContradiction('Payload helper exited before GO') from exc
                    def report_capture():
                        with store.transaction(create=False, wait=time.sleep) as con:
                            current = store.get(con, job_id)
                            ownership_hold.unavailable(store, con, current, 'guardian', 'anchor.capture', time.monotonic())
                    ownership_hold.retry_read(report_capture)
                    raise OwnershipUnavailable('anchor.capture', 'Initial anchor identity is unreadable') from exc
            group = ownership_hold.retry_read(capture_anchor)
            authority = ownership_hold.WorkerAuthority(store, job, group, leader)
            # Persist the newly created, unreaped anchor before GO. The final
            # admission transaction can now issue GO while it owns the queue lock.
            def register_anchor():
                with store.transaction(create=False, wait=authority.queue_wait) as con:
                    current = store.get(con, job_id)
                    launch_state = current.get('ownership_resume_status') or current['status']
                    if (current['claim'] == claim and current['worker'] == authority.worker
                            and current['attempt_id'] == initial['attempt_id'] and current['group'] is None
                            and launch_state in {'Starting', 'Cancelling'}):
                        current['group'] = group
                        store.save(con, current)
            ownership_hold.retry_read(register_anchor, wait=authority.queue_wait)
            refusal = None
            while True:
                try:
                    generation = authority.generation
                    before = store.snapshot()
                    pending_ids = [(j['id'], j['order'], j['status']) for j in before['jobs'] if j['status'] in PENDING]
                    if before.get('scheduler', {}).get('mode') == 'bounded-sharing':
                        guard, evidence = sharing.AdmissionSampler().collect(store, source='supervisor-admission', wait=True)
                    else:
                        guard = activity.DEFAULT_OBSERVER.scan(store, before, source='supervisor-admission')
                        evidence = None
                    # Byte/provenance checks run once at actual admission, never on a UI poll.
                    final_blocker = launch_blocker(job, final=True) if (job.get('molecular_submission')
                        or job.get('node_submission', {}).get('successor')
                        or job.get('node_submission', {}).get('study')) else None
                    authority.wait(before_admission=True)
                    if authority.generation != generation:
                        continue
                    with store.transaction() as con:
                        record_guard(store, con, guard)
                        job = store.get(con, job_id)
                        if job.get('ownership_holds'):
                            continue  # Recheck ownership and admission with the original pipe.
                        control = store.control(con)
                        pending_now = [(j['id'], j['order'], j['status']) for j in store.rows(con) if j['status'] in PENDING]
                        decision_state = {'jobs': store.rows(con), 'scheduler': control.get('scheduler', {}),
                                          'controller': {**control, 'online': dispatch_authority(store, control), 'stale': False}}
                        decision = sharing.evaluate(decision_state, guard, evidence, candidate_id=job_id, final=True)
                        bound = (job['claim'] == claim and job['worker'] == authority.worker and job['group'] == group
                                 and job['attempt_id'] == initial['attempt_id'])
                        admitted = (bound and job['status'] == 'Starting' and not job.get('termination_requested') and
                                    not job['cancel_requested'] and dispatch_authority(store, control) and
                                    pending_ids == pending_now and decision['permitted'] and not final_blocker)
                        if not admitted:
                            refusal = _AdmissionRefusal(store, job, authority, leader,
                                attempt_id=initial['attempt_id'], blocker=final_blocker,
                                reason=decision['reason'] if not decision['permitted'] else 'Admission controls/order changed',
                                coordinator=coordinator)
                            refusal.prepare(con, job)
                        elif not job.get('suspension') or coordinator is not None:
                            job.update(group=group, started_at=time.time(), reason="", status="Run")
                            store.save(con, job)
                            store.event(con, job["id"], "Execution started; group ownership persisted")
                            # Study/successor preflight above may read this queue through
                            # a second connection. Reserve the writer throughout admission,
                            # then retain the COMMIT's exclusive lock through GO.
                            if con.execute('PRAGMA locking_mode=EXCLUSIVE').fetchone()[0] != 'exclusive':
                                raise JobsError('Cannot retain queue authority through payload GO')
                            _commit_job(store, con, job, authority.queue_wait)
                            leader.stdin.write(b"GO\n")
                            leader.stdin.flush()
                            break
                    if refusal is not None:
                        refusal.finish()
                        return
                    from .suspension import Coordinator
                    coordinator = Coordinator(store, job, group)
                    coordinator.authority = authority
                    # Preparing the channel may take time. Recheck admission and both
                    # owners after preparation and before committing the GO decision.
                except Exception as exc:
                    if isinstance(exc, (OwnershipContradiction, ownership_hold.HoldDriverMissing)):
                        raise
                    if refusal is not None:
                        # The refusal's COMMIT may have succeeded. Its own
                        # driver reads back before close/requeue; never resume GO.
                        refusal.finish()
                        return
                    # Every read before GO is repeated from fresh admission
                    # evidence after the failed transaction has rolled back.
                    failure = exc if is_unavailable(exc) else OwnershipUnavailable('admission.read', str(exc))
                    authority.read_domain(lambda: _read_job(store, job_id), first_failure=failure)
            cancel_at = None
            delivered = set()
            code = None
            session = job.get('env', {}).get('CMW_JOBS_OWN_SESSION') == '1'
            def members():
                return group_members(leader.pid, session=True) if session else group_members(leader.pid)

            while True:
                try:
                    job = authority.wait()
                    observed_members = authority.read_domain(members)
                    receipt_evidence = authority.read_domain(lambda: _receipt(receipt))
                    if coordinator and not job.get('cancel_requested') and not job.get('termination_requested'):
                        coordinator.tick(job, receipt_present=receipt_evidence is not None)
                        job = coordinator.current()
                    terminating = job['cancel_requested'] or job.get('termination_requested')
                    if coordinator and terminating:
                        # A completed runner may have exited before a concurrent
                        # cancel request. Genuine receipt/drain permits normal release.
                        if not (receipt_evidence is not None and coordinator.ready_to_release()):
                            coordinator.cancel_step(job, CANCEL_GRACE)
                        job = coordinator.current()
                    elif terminating:
                        # The unreaped direct child pins this PID/group; birth identity
                        # additionally protects signalling from corrupted stale metadata.
                        control_clock = time.monotonic() - authority.hold_seconds
                        if cancel_at is None or (control_clock - cancel_at > CANCEL_GRACE and observed_members):
                            signum = signal.SIGTERM if cancel_at is None else signal.SIGKILL
                            def send(pgid, signum, witness=None):
                                while True:
                                    attempted = False
                                    try:
                                        with _worker_intent(store, job, None, authority.queue_wait):
                                            if witness is not None and not witness():
                                                return False
                                            if (pgid, signum) not in delivered:
                                                attempted = True
                                                os.killpg(pgid, signum)
                                                delivered.add((pgid, signum))
                                            return True
                                    except Exception as exc:
                                        if attempted or not is_unavailable(exc):
                                            raise
                                        # Drop the queue lock before holding/retrying the
                                        # read. Reacquire it and recheck before delivery.
                                        authority.read_domain(witness or (lambda: _read_job(store, job_id)), first_failure=exc)
                            try:
                                authority.wait()
                                release_ready = False
                                if session:
                                    outcome = signal_session(
                                        group, signum, check_authority=authority.wait,
                                        read_domain=authority.read_domain, send_group=send,
                                        anchor_ready_for_release=lambda: _receipt(receipt) is not None and set(members()) == {leader.pid})
                                    release_ready = outcome is False
                                else:
                                    try:
                                        send(leader.pid, signum)
                                    except OSError as exc:
                                        def observe_denied_send():
                                            if _receipt(receipt) is not None and set(members()) == {leader.pid}:
                                                return True
                                            raise OwnershipUnavailable('group_send', 'Denied delivery awaits natural receipt and drainage')
                                        authority.read_domain(observe_denied_send,
                                            first_failure=OwnershipUnavailable('group_send', str(exc)))
                                        release_ready = True
                            except RequestSuperseded:
                                continue  # Observe the latest intent; retain the pipe.
                            if signum == signal.SIGTERM:
                                cancel_at = time.monotonic() - authority.hold_seconds
                            elif not release_ready:
                                completion = _Completion(store, job, authority, leader, session=session)
                                completion.report('termination evidence')
                                completion.finish("Cancelled" if job['cancel_requested'] else "Fail",
                                                  137 if job['cancel_requested'] else 124, "Termination confirmed",
                                                  termination_signal=signal.SIGKILL)
                                return
                    receipt_evidence = authority.read_domain(lambda: _receipt(receipt))
                    if receipt_evidence is not None:
                        code = receipt_evidence[0]['exit_code']
                        generation = authority.generation
                        drained = (coordinator.ready_to_release() if coordinator
                                   else set(authority.read_domain(members)) == {leader.pid})
                        if drained:
                            authority.wait()
                            if authority.generation != generation:
                                continue  # Drainage evidence predates an observation gap.
                            completion = _Completion(store, job, authority, leader,
                                                     coordinator=coordinator, session=session)
                            try:
                                completion.release(receipt, receipt_evidence)
                            except RequestSuperseded:
                                completion.abandon_preparation()
                                continue  # No RELEASE occurred; re-read superseding intent.
                            if job['cancel_requested']:
                                completion.finish('Cancelled', code, 'Termination confirmed')
                            elif job.get('termination_requested'):
                                completion.finish('Fail', 124, 'Declared execution deadline expired')
                            else:
                                completion.finish("Done" if code == 0 else "Fail", code,
                                                  "Execution contract completed; scientific checks not evaluated" if code == 0 else f"Exit code {code}")
                            return
                    time.sleep(INTERVAL)
                except CompletionDomainUnobserved as exc:
                    _wait_unobserved_domain(store, job, authority, str(exc))
                    return
                except RequestSuperseded:
                    continue
                except Exception as exc:
                    if isinstance(exc, (OwnershipContradiction, ownership_hold.HoldDriverMissing)):
                        raise
                    # Delivered signals remain in the per-attempt set. Re-read
                    # current intent; never restart the worker or its GO phase.
                    failure = exc if is_unavailable(exc) else OwnershipUnavailable('worker.read', str(exc))
                    authority.read_domain(lambda: _read_job(store, job_id), first_failure=failure)
        except OwnershipContradiction as exc:
            def record_contradiction():
                with store.transaction(create=False, wait=time.sleep) as con:
                    current = store.get(con, job_id)
                    if current['claim'] == claim and current['status'] in ACTIVE:
                        if current['status'] != 'Unknown':
                            mark_unknown(store, con, current, str(exc))
            ownership_hold.retry_read(record_contradiction)
        except Exception as exc:
            # This is an invariant/programming failure outside the retryable
            # read phases, not evidence of lost ownership. Keep the original
            # pipe while a visible diagnostic driver re-observes authority.
            delay = .25
            while True:
                try:
                    with store.transaction(create=False, wait=time.sleep) as con:
                        current = store.get(con, job_id)
                        ownership_hold.unavailable(store, con, current, 'worker.internal',
                                                   type(exc).__name__ + ': ' + str(exc), time.monotonic())
                    print(f'Worker recovery invariant requires attention: {exc}', file=sys.stderr, flush=True)
                    result = observe_owner(current.get('group'))
                    if result.status == OwnershipStatus.GONE:
                        with store.transaction(create=False, wait=time.sleep) as con:
                            current = store.get(con, job_id)
                            mark_unknown(store, con, current, 'Group leader disappeared during blocked internal recovery')
                        break
                except Exception as observation_error:
                    if isinstance(observation_error, OwnershipContradiction):
                        raise
                time.sleep(delay)
                delay = min(delay * 2, ownership_hold.MAX_RETRY)
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
