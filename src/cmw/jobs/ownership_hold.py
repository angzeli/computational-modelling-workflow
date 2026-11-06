"""Persist a recoverable control hold without releasing the guardian's pipe."""
from __future__ import annotations

import time

import psutil

from .freshness import age_seconds, clock_stamp
from .ownership import OwnershipStatus, OwnershipUnavailable, lock_held, observe_owner
from .store import JobsError


# The first failure blocks control immediately. A minute of continuous
# uncertainty merits operator attention, even when the controller is offline.
UNKNOWN_AFTER = 60.0
MAX_RETRY = 5.0


def unavailable(store, con, job, observer, check, now):
    holds = job.setdefault('ownership_holds', {})
    current = holds.get(observer)
    observed = clock_stamp(now)
    if current is None:
        holds[observer] = {'check': check, 'since_clock': observed}
        store.event(con, job['id'], f'Control suspended: {observer} cannot verify {check}')
        store.save(con, job)
        return
    if current['check'] != check:
        current['check'] = check
        store.event(con, job['id'], f'Control suspended: {observer} cannot verify {check}')
        store.save(con, job)
    elapsed = age_seconds(current.get('since_clock'), observed)
    if (elapsed is None or elapsed >= UNKNOWN_AFTER) and job['status'] != 'Unknown':
        job['ownership_resume_status'] = job['status']
        job['status'] = 'Unknown'
        reason = f'Control suspended: {observer} cannot verify {check}'
        if elapsed is None:
            reason += '; hold duration cannot be established on this boot'
        job['reason'] = reason + '; retaining supervision'
        store.save(con, job)
        store.event(con, job['id'], job['reason'])


def recovered(store, con, job, observer):
    holds = job.get('ownership_holds')
    if not holds or observer not in holds:
        return
    del holds[observer]
    if not holds:
        del job['ownership_holds']
        previous = job.pop('ownership_resume_status', None)
        if previous and job['status'] == 'Unknown':
            job.update(status=previous, reason='Ownership verified; supervision resumed')
    store.save(con, job)
    store.event(con, job['id'], f'Ownership verified: {observer} control hold cleared')


def blocks_replacement(owner):
    """An empty owner slot or proven loss allows an independent lock check."""
    return owner is not None and observe_owner(owner).status != OwnershipStatus.GONE


class WorkerAuthority:
    """Only the worker holding the original pipe can resume this supervision."""

    def __init__(self, store, job, anchor, child):
        self.store = store
        self.job_id = job['id']
        self.claim = job['claim']
        self.worker = job['worker']
        self.anchor = anchor
        self.child = child
        self.generation = 0
        self.hold_seconds = 0.0

    def _backoff(self, delay):
        started = time.monotonic()
        time.sleep(delay)
        self.hold_seconds += time.monotonic() - started

    def queue_wait(self, delay):
        self.generation += 1
        self._backoff(delay)

    def read_domain(self, observation, *, first_failure=None):
        """Retry only a read. Never replay a coordinator command or signal."""
        delay = .25
        held = False
        while True:
            try:
                if first_failure is not None:
                    error, first_failure = first_failure, None
                    raise error  # The final probe failed inside a delivery lock.
                value = observation()
            except (OwnershipUnavailable, PermissionError, psutil.AccessDenied) as exc:
                self.wait(retrying_domain=True)
                with self.store.transaction(create=False, wait=self.queue_wait) as con:
                    job = self.store.get(con, self.job_id)
                    check = exc.deciding_check if isinstance(exc, OwnershipUnavailable) else 'domain.access'
                    unavailable(self.store, con, job, 'domain', check, time.monotonic())
                held = True
                self.generation += 1
                self._backoff(delay)
                delay = min(delay*2, MAX_RETRY)
            else:
                if held:
                    with self.store.transaction(create=False, wait=self.queue_wait) as con:
                        recovered(self.store, con, self.store.get(con, self.job_id), 'domain')
                    held = False
                    self.wait()
                    continue  # Read again after the recovery transaction, before acting.
                return value

    def wait(self, *, before_admission=False, retrying_domain=False):
        delay = .25
        while True:
            generation = self.generation
            worker = observe_owner(self.worker)
            group = observe_owner(self.anchor)
            if group.status == OwnershipStatus.UNVERIFIABLE and self.child.poll() is not None:
                # waitpid confirms this exact child exited (including zombies).
                raise JobsError('Group leader disappeared: direct child exit confirmed by waitpid')
            with self.store.transaction(create=False, wait=self.queue_wait) as con:
                if self.generation != generation:
                    continue  # Contention delayed the ownership read; observe again.
                job = self.store.get(con, self.job_id)
                if job['claim'] != self.claim or job['worker'] != self.worker or (
                        job.get('group') is not None and job['group'] != self.anchor):
                    if before_admission:
                        return job  # The final admission transaction refuses the changed claim.
                    raise JobsError('Recorded worker or group authority changed; refusing control')
                if before_admission and job['status'] not in {'Starting', 'Cancelling'} and not job.get('ownership_resume_status'):
                    return job
                if group.status == OwnershipStatus.GONE:
                    raise JobsError(f'Group leader disappeared ({group.deciding_check})')
                if worker.status == OwnershipStatus.GONE:
                    raise JobsError(f'Recorded worker is gone ({worker.deciding_check})')
                failed = (f'worker.{worker.deciding_check}' if worker.status != OwnershipStatus.VERIFIED else
                          f'anchor.{group.deciding_check}' if group.status != OwnershipStatus.VERIFIED else None)
                if failed:
                    unavailable(self.store, con, job, 'guardian', failed, time.monotonic())
                else:
                    recovered(self.store, con, job, 'guardian')
                    if 'controller' in job.get('ownership_holds', {}):
                        try:
                            locked = lock_held(self.store.root / 'attempts' / job['attempt_id'] / 'worker.lock', strict=True)
                        except OSError:
                            locked = False
                        if locked:
                            recovered(self.store, con, job, 'controller')
                    holds = job.get('ownership_holds', {})
                    independently_unknown = job['status'] == 'Unknown' and not job.get('ownership_resume_status')
                    if not independently_unknown and (not holds or (retrying_domain and set(holds) == {'domain'})):
                        return job
            # No queue transaction is held while waiting. Never signal, write
            # GO/RELEASE, reap the anchor or close stdin from this retry loop.
            self.generation += 1
            self._backoff(delay)
            delay = min(delay*2, MAX_RETRY)
