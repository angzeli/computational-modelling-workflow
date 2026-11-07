"""Failure-safe cleanup for this task's private synthetic ORCA process tree.

Queue cancellation is advisory here: Unknown refuses it, and an ownership hold
can delay it. The fixture still owns its captured process identities. Cleanup
never turns either condition into production authority or a terminal job state.
"""
from __future__ import annotations

import json
import os
import signal
import time

import psutil

from cmw.jobs import runtime
from cmw.jobs.ownership import OwnershipStatus, identity, observe_owner
from cmw.jobs.process_identity import ConfirmedProcessAbsence
from cmw.jobs.store import JobsError


class OwnedProcessCleanup:
    def __init__(self, store, root):
        self.store, self.root = store, root
        self.records = {}
        self.handles = []
        self.refusals = []
        self.complete = False
        self.controller = None

    def remember(self, record):
        if record:
            self.records[json.dumps(record, sort_keys=True)] = record

    def acquired(self, process):
        # Keep the unreaped direct-child handle even if identity capture fails.
        self.handles.append(process)
        try:
            self.remember(identity(process.pid))
        except (psutil.NoSuchProcess, ConfirmedProcessAbsence):
            process.wait(timeout=2)
        return process

    def _queue_exists(self):
        # Path.exists() suppresses unreadable-path errors on Python 3.14.
        # Missing inventory must not be mistaken for proof that nothing ran.
        try:
            self.store.path.stat()
        except FileNotFoundError:
            return False
        return True

    def _queue_records(self):
        if self._queue_exists():
            with self.store.transaction(create=False) as con:
                control = self.store.control(con)
                self.controller = control.get('owner') or self.controller
                self.remember(self.controller)
                for job in self.store.rows(con):
                    for key in ('worker', 'group'):
                        self.remember(job.get(key))
                    path = self.store.root/'attempts'/job['attempt_id']/'suspension'/'registration.json'
                    try:
                        record = json.loads(path.read_text())
                    except FileNotFoundError:
                        continue
                    for key in ('runner', 'root'):
                        self.remember(record.get(key))
                    for owner in record.get('protected', []):
                        self.remember(owner)

    def _descendants(self, owner):
        if observe_owner(owner).status != OwnershipStatus.VERIFIED:
            return
        try:
            children = psutil.Process(owner['pid']).children(recursive=True)
            for child in children:
                try:
                    record = identity(child.pid)
                    parents = [parent.pid for parent in child.parents()]
                    if (owner['pid'] in parents and identity(child.pid) == record
                            and observe_owner(owner).status == OwnershipStatus.VERIFIED):
                        self.remember(record)
                except (psutil.NoSuchProcess, ConfirmedProcessAbsence):
                    pass
        except psutil.NoSuchProcess:
            pass

    def collect(self):
        errors = []
        def capture(operation):
            try:
                operation()
            except (FileNotFoundError, psutil.NoSuchProcess, ConfirmedProcessAbsence):
                pass
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
        for process in self.handles:
            if process.poll() is None:
                capture(lambda process=process: self.remember(identity(process.pid)))
        capture(self._queue_records)
        # A queue read failure must not suppress independent fixture evidence.
        for path in [self.root/'driver.json', *self.root.glob('module-*.json')]:
            capture(lambda path=path: self.remember(json.loads(path.read_text()).get('owner')))
        # Capture children before stopping their parents. Never discover by
        # executable name or signal an identity inferred from a numeric PID.
        for owner in list(self.records.values()):
            capture(lambda owner=owner: self._descendants(owner))
        if errors:
            raise AssertionError('Fixture inventory incomplete: ' + '; '.join(errors))

    def reap(self):
        for process in self.handles:
            process.poll()
        runtime.reap_detached()

    def remaining(self, *, controller=True):
        self.reap()
        result = []
        for owner in self.records.values():
            if controller or owner != self.controller:
                observed = observe_owner(owner)
                if observed.status != OwnershipStatus.GONE:
                    result.append((owner, observed))
        return result

    def wait_gone(self, seconds, *, controller=True):
        limit = time.monotonic() + seconds
        while self.remaining(controller=controller) and time.monotonic() < limit:
            time.sleep(.025)
        return not self.remaining(controller=controller)

    def send(self, signum, *, controller=False):
        # Leaves first lets their waiting parents reap before their own exit.
        errors = []
        for owner in reversed(list(self.records.values())):
            if not controller and owner == self.controller:
                continue
            if observe_owner(owner).status != OwnershipStatus.VERIFIED:
                continue
            try:
                if signum == signal.SIGCONT and psutil.Process(owner['pid']).status() not in {
                        psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP}:
                    continue
                if observe_owner(owner).status != OwnershipStatus.VERIFIED:
                    continue
                os.kill(owner['pid'], signum)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
            except Exception as exc:
                errors.append(f'{owner}: {type(exc).__name__}: {exc}')
        if errors:
            raise AssertionError('Fixture signal refused: ' + '; '.join(errors))

    def close(self):
        if self.complete:
            return
        errors = []
        def step(operation):
            try:
                return operation()
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')

        step(self.collect)
        if step(self._queue_exists):
            def cancel():
                with self.store.transaction(create=False) as con:
                    jobs = self.store.rows(con)
                for job in jobs:
                    if job['status'] in {'Starting', 'Run', 'Cancelling', 'Unknown'}:
                        try:
                            self.store.change(job['id'], 'cancel', confirm=True)
                        except JobsError as exc:
                            self.refusals.append(str(exc))
            step(cancel)
        # Do not wait for a terminal row: that is the very assertion which may
        # have failed. The cleanup's own captured identities are its authority.
        step(self.collect)
        step(lambda: self.send(signal.SIGTERM))
        step(lambda: self.send(signal.SIGCONT))
        step(lambda: self.wait_gone(.5, controller=False))
        step(self.collect)
        step(lambda: self.send(signal.SIGKILL))
        step(lambda: self.wait_gone(3, controller=False))
        if step(self._queue_exists):
            step(lambda: runtime.stop(self.store))
        step(lambda: self.wait_gone(2))
        # A controller that failed before startup/queue publication is still
        # captured by acquired(), and must not survive that setup failure.
        step(lambda: self.send(signal.SIGTERM, controller=True))
        step(lambda: self.wait_gone(.5))
        step(lambda: self.send(signal.SIGKILL, controller=True))
        step(lambda: self.wait_gone(3))
        for process in self.handles:
            step(lambda process=process: process.wait(timeout=1))
        step(self.reap)
        remaining = self.remaining()
        self.complete = not remaining and not errors
        if not self.complete:
            detail = [(owner, result.status, result.deciding_check) for owner, result in remaining]
            raise AssertionError(f'Owned fixture cleanup incomplete: {detail}; errors={errors}; root={self.root}')
