"""Persist a recoverable control hold without releasing the guardian's pipe."""
from __future__ import annotations

from dataclasses import dataclass
import sys
import time

import psutil

from .freshness import age_seconds, clock_stamp
from .ownership import (ObservationFinished, OwnershipContradiction, OwnershipStatus,
                        OwnershipUnavailable, is_unavailable, lock_held, observe_owner)
from .store import JobsError


# The first failure blocks control immediately. A minute of continuous
# uncertainty merits operator attention, even when the controller is offline.
ATTENTION_AFTER = 60.0
MAX_RETRY = 5.0


def _read_failure(error):
    if is_unavailable(error):
        return True
    # These exceptions carry an observed outcome or a caller-owned next phase.
    # Unexpected exceptions from a *read* are diagnostics, not contradictions.
    return not isinstance(error, (JobsError, ProcessLookupError, psutil.NoSuchProcess))


def retry_read(observation, *, wait=None, on_unavailable=None):
    """Retry only the supplied read, without requiring a live process anchor."""
    delay = .25
    while True:
        try:
            return observation()
        except Exception as exc:
            if not _read_failure(exc):
                raise
            if on_unavailable is not None:
                try:
                    on_unavailable(exc)
                except Exception as reporting:
                    if not _read_failure(reporting):
                        raise
                    # Reporting is not the owner of this read's retry lifetime.
                    # The next failed read attempts the diagnostic again.
            (time.sleep if wait is None else wait)(delay)
            delay = min(delay * 2, MAX_RETRY)


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
    if (elapsed is None or elapsed >= ATTENTION_AFTER) and not current.get('attention'):
        current['attention'] = True
        reason = f'Control suspended: {observer} cannot verify {check}'
        if elapsed is None:
            reason += '; hold duration cannot be established on this boot'
        current['reason'] = reason + '; attention required; retaining supervision'
        store.save(con, job)
        store.event(con, job['id'], current['reason'])


def recovered(store, con, job, observer, *, verified=True):
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
    outcome = 'Ownership verified' if verified else 'Ownership observation resolved'
    store.event(con, job['id'], f'{outcome}: {observer} control hold cleared')


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
        self.completing = False
        self._reads = []
        self._read_reports = {}

    def _backoff(self, delay):
        started = time.monotonic()
        time.sleep(delay)
        self.hold_seconds += time.monotonic() - started

    def queue_wait(self, delay):
        self.generation += 1
        self._backoff(delay)

    def _report_read(self, check, error):
        now = time.monotonic()
        report = self._read_reports.get(check)
        attention = report is not None and now - report[0] >= ATTENTION_AFTER
        if report is None or (attention and not report[1]):
            self._read_reports[check] = (now if report is None else report[0], attention)
            try:
                print(f'Control suspended: {check}: {error}; '
                      + ('attention required; ' if attention else '') + 'retrying observation',
                      file=sys.stderr, flush=True)
            except OSError:
                pass  # A failed diagnostic write cannot dispose of the driver.

    def _domain_state(self, *, verified=True):
        """Publish an idempotent hold transition; its read driver already exists."""
        def publish():
            with self.store.transaction(create=False, wait=self.queue_wait) as con:
                job = self.store.get(con, self.job_id)
                if job['claim'] != self.claim or job['worker'] != self.worker:
                    raise OwnershipContradiction('Recorded worker authority changed while recording a hold')
                pending = [read for read in self._reads if read.held]
                if pending:
                    error = pending[-1].unavailable
                    check = getattr(error, 'deciding_check', 'domain.' + type(error).__name__)
                    unavailable(self.store, con, job, 'domain', check, time.monotonic())
                else:
                    recovered(self.store, con, job, 'domain', verified=verified)
        retry_read(publish, wait=self.queue_wait,
                   on_unavailable=lambda error: self._report_read('domain.queue', error))

    def _resolve_read(self, read, *, verified=True):
        if read.held:
            read.held = False
            self._domain_state(verified=verified)

    def _drive_read(self, read):
        while not read.finished:
            generation = self.generation
            try:
                read.running = True
                if read.first_failure is not None:
                    error, read.first_failure = read.first_failure, None
                    raise error
                value = read.observation()
            except Exception as exc:
                if not _read_failure(exc):
                    self._resolve_read(read, verified=False)
                    read.finished = True
                    raise
                read.unavailable, read.held = exc, True
                self.generation += 1
                self._domain_state()
                if not is_unavailable(exc):
                    self._report_read('domain.internal', exc)
            else:
                if not read.held:
                    if self.generation != generation:
                        self.wait(retrying_domain=True)
                        continue  # A nested read held; discard the enclosing partial result.
                    read.finished = True
                    return value
                self._resolve_read(read)
                # A recovery transaction/another hold can delay the observation.
                # Recheck authority and then obtain a new result before acting.
                self.wait(retrying_domain=True)
                continue
            finally:
                read.running = False
            self.wait(retrying_domain=True)
            self._backoff(read.delay)
            read.delay = min(read.delay * 2, MAX_RETRY)

    def read_domain(self, observation, *, first_failure=None):
        """The operation owns its hold until a result or explicit phase exit."""
        read = _ReadOperation(observation, first_failure)
        self._reads.append(read)
        try:
            return self._drive_read(read)
        except Exception as exc:
            if not _read_failure(exc):
                self._resolve_read(read, verified=False)
                read.finished = True
            raise
        finally:
            if read.finished:
                if read in self._reads:
                    self._reads.remove(read)

    def wait(self, *, before_admission=False, retrying_domain=False):
        delay = .25
        while True:
            try:
                result, driver = self._wait_observation(before_admission, retrying_domain)
            except Exception as exc:
                if not _read_failure(exc):
                    raise
                self._report_read('authority.read', exc)
                result, driver = None, None
            if result is not None:
                return result
            if driver is not None:
                try:
                    self._drive_read(driver)
                finally:
                    if driver.finished and driver in self._reads:
                        self._reads.remove(driver)
                continue
            self.generation += 1
            self._backoff(delay)
            delay = min(delay * 2, MAX_RETRY)

    def _wait_observation(self, before_admission, retrying_domain):
        generation = self.generation
        worker = observe_owner(self.worker)
        group = observe_owner(self.anchor)
        if group.status == OwnershipStatus.UNVERIFIABLE and self.child.poll() is not None:
            # waitpid confirms this exact child exited (including zombies).
            raise OwnershipContradiction('Group leader disappeared: direct child exit confirmed by waitpid')
        with self.store.transaction(create=False, wait=self.queue_wait) as con:
            if self.generation != generation:
                return None, None  # Contention delayed the ownership read; observe again.
            job = self.store.get(con, self.job_id)
            if job['claim'] != self.claim or job['worker'] != self.worker or (
                    job.get('group') is not None and job['group'] != self.anchor):
                if before_admission:
                    return job, None  # The final admission transaction refuses the changed claim.
                raise OwnershipContradiction('Recorded worker or group authority changed; refusing control')
            if before_admission and job['status'] not in {'Starting', 'Cancelling'} and not job.get('ownership_resume_status'):
                return job, None
            if group.status == OwnershipStatus.GONE:
                raise OwnershipContradiction(f'Group leader disappeared ({group.deciding_check})')
            if worker.status == OwnershipStatus.GONE:
                raise OwnershipContradiction(f'Recorded worker is gone ({worker.deciding_check})')
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
                if self.completing:
                    holds = {name: value for name, value in holds.items() if name != 'completion'}
                if 'domain' in holds and not retrying_domain:
                    drivers = [read for read in reversed(self._reads) if read.held and not read.running]
                    if drivers:
                        return None, drivers[0]
                    error = OwnershipUnavailable('hold.driver',
                        'Ownership hold invariant violated: domain hold has no re-evaluation driver')
                    self._report_read('hold.driver', error)
                    raise HoldDriverMissing(str(error))
                independently_unknown = job['status'] == 'Unknown' and not job.get('ownership_resume_status')
                if not independently_unknown and (not holds or (retrying_domain and set(holds) == {'domain'})):
                    return job, None
        # No queue transaction is held while waiting. Never signal, write
        # GO/RELEASE, reap the anchor or close stdin from this retry loop.
        return None, None


class HoldDriverMissing(ObservationFinished):
    """A persisted hold without an operation is an immediate runtime invariant error."""


@dataclass(eq=False)
class _ReadOperation:
    observation: object
    first_failure: Exception | None = None
    unavailable: Exception | None = None
    held: bool = False
    running: bool = False
    finished: bool = False
    delay: float = .25
