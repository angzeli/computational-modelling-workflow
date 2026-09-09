"""Ephemeral advisory usage of identities already observed by Jobs.

No scheduler state, reservations, or ownership decisions depend on these reads.
CPU baselines belong to one viewing client and use monotonic elapsed time.
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time

import psutil

from .ownership import identity, observed_group_members
from .store import ACTIVE, JobsError

MAX_AGE = 5.0
SAMPLE_BUDGET = 0.75


def empty_usage(reason='No verified current members'):
    return dict(cpu_cores=None, rss_bytes=None, cpu_quality='unavailable',
                memory_quality='unavailable', cpu_members=0, memory_members=0,
                total_members=0, observed_at=None, age_seconds=None, stale=False, reason=reason)


def age_usage(value, now=None):
    """A pure presentation projection; never reads the operating system."""
    result = copy.deepcopy(value)
    if result and result.get('observed_at') is not None:
        result['age_seconds'] = max(0, (time.time() if now is None else now)-result['observed_at'])
        result['stale'] = bool(result.get('stale') or result['age_seconds'] > MAX_AGE)
    return result


class Sampler:
    """At most one in-flight read, with atomic acceptance of samples/baselines.

A late read after a timeout cannot overwrite the accepted client snapshot or
advance its CPU baseline. Access failures leave guard evidence untouched.
"""
    def __init__(self, *, process_factory=psutil.Process, identity_provider=identity,
                 cpu_times=psutil.cpu_times, virtual_memory=psutil.virtual_memory,
                 monotonic=time.monotonic, wall_time=time.time):
        self.process_factory = process_factory
        self.identity = identity_provider
        self.cpu_times = cpu_times
        self.virtual_memory = virtual_memory
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.baselines = {}
        self.machine_baseline = None
        self.last = None
        self.thread = None
        self.lock = threading.Lock()

    def sample(self, store, state, guard, pids):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return self._failed(state, guard, 'Previous telemetry read still in progress')
            box = {}
            def run():
                try:
                    box['result'] = self._collect(store, state, guard, pids,
                                                  self.monotonic()+SAMPLE_BUDGET)
                except Exception as exc:
                    box['error'] = f'Telemetry unavailable: {type(exc).__name__}: {exc}'
            self.thread = threading.Thread(target=run, daemon=True, name='cmw-usage')
            self.thread.start()
            self.thread.join(SAMPLE_BUDGET)
            if self.thread.is_alive() or 'error' in box:
                return self._failed(state, guard, box.get('error', 'Telemetry read timed out'))
            result, baselines, machine_baseline = box['result']
            self.baselines, self.machine_baseline = baselines, machine_baseline
            self.last = copy.deepcopy(result)
            return result

    def _failed(self, state, guard, reason):
        last = self.last or {}
        def stale(value, fallback):
            result = age_usage(value or fallback, self.wall_time())
            result.update(stale=True, reason=reason)
            return result
        return {
            'machine_usage': stale(last.get('machine_usage'), self._empty_machine()),
            'jobs': {j['attempt_id']: stale(last.get('jobs', {}).get(j['attempt_id']), empty_usage())
                     for j in state['jobs'] if j['status'] in ACTIVE},
            'external': {o['id']: stale(last.get('external', {}).get(o['id']), empty_usage())
                         for o in guard['observations']}}

    @staticmethod
    def _empty_machine():
        return dict(cpu_percent=None, ram_used_bytes=None, ram_total_bytes=None,
                    cpu_quality='unavailable', memory_quality='unavailable',
                    observed_at=None, age_seconds=None, stale=False, reason='')

    def _machine(self):
        result, baseline = self._empty_machine(), None
        reasons = []
        try:
            times = self.cpu_times()._asdict()
            # Linux guest counters are included in user/nice; iowait is idle.
            total = sum(v for k, v in times.items() if k not in {'guest', 'guest_nice'})
            idle = times.get('idle', 0) + times.get('iowait', 0)
            baseline = (total, idle, self.monotonic())
            before = self.machine_baseline
            if (before and 0 < baseline[2]-before[2] <= MAX_AGE and
                    total > before[0] and idle >= before[1]):
                result['cpu_percent'] = min(100.0, max(0.0, 100*(1-(idle-before[1])/(total-before[0]))))
                result['cpu_quality'] = 'fresh'
            else:
                result['cpu_quality'] = 'warming-up'
        except (psutil.Error, OSError, ValueError) as exc:
            reasons.append(f'Machine CPU: {type(exc).__name__}')
        try:
            memory = self.virtual_memory()
            result.update(ram_used_bytes=max(0, int(memory.total-memory.available)),
                          ram_total_bytes=int(memory.total), memory_quality='fresh')
        except (psutil.Error, OSError, ValueError) as exc:
            reasons.append(f'Machine memory: {type(exc).__name__}')
        result.update(observed_at=self.wall_time(), age_seconds=0, reason='; '.join(reasons))
        return result, baseline

    def _workload(self, members, baseline_out, deadline):
        result = empty_usage()
        result['total_members'] = len(members)
        rates, memory, new = [], [], 0
        reasons = []
        for member in members:
            if self.monotonic() > deadline:
                raise TimeoutError('Telemetry sample budget exceeded')
            owner = member['identity']
            key = json.dumps(owner, sort_keys=True)
            try:
                p = self.process_factory(owner['pid'])
                if self.identity(owner['pid']) != owner or p.status() == psutil.STATUS_ZOMBIE:
                    reasons.append('Member identity changed or became zombie')
                    continue
                cpu, rss = None, None
                try:
                    times = p.cpu_times()
                    cpu = float(times.user + times.system)
                    if not math.isfinite(cpu) or cpu < 0:
                        cpu = None
                except (psutil.AccessDenied, PermissionError):
                    reasons.append('Member CPU inaccessible')
                try:
                    rss = max(0, int(p.memory_info().rss))
                except (psutil.AccessDenied, PermissionError):
                    reasons.append('Member RSS inaccessible')
                moment = self.monotonic()
                if self.identity(owner['pid']) != owner or p.status() == psutil.STATUS_ZOMBIE:
                    reasons.append('Member identity changed during usage read')
                    continue
                if member.get('pgid') is not None and os.getpgid(owner['pid']) != member['pgid']:
                    reasons.append('Member left verified process group')
                    continue
                if rss is not None:
                    memory.append(rss)
                if cpu is not None:
                    baseline_out[key] = (cpu, moment)
                    before = self.baselines.get(key)
                    if before and 0 < moment-before[1] <= MAX_AGE and cpu >= before[0]:
                        rates.append((cpu-before[0])/(moment-before[1]))
                    else:
                        new += 1
            except (psutil.NoSuchProcess, ProcessLookupError):
                result['total_members'] -= 1
            except (psutil.Error, OSError) as exc:
                reasons.append(f'Member usage unavailable: {type(exc).__name__}')
        total = result['total_members']
        result.update(cpu_cores=sum(rates) if rates else None,
                      rss_bytes=sum(memory) if memory else None,
                      cpu_members=len(rates), memory_members=len(memory))
        result['cpu_quality'] = ('fresh' if rates and len(rates) == total else 'partial' if rates else
                                 'warming-up' if new and new == total else 'unavailable')
        result['memory_quality'] = ('fresh' if memory and len(memory) == total else
                                    'partial' if memory else 'unavailable')
        if new:
            reasons.append(f'{new} CPU baseline(s) warming up')
        result.update(observed_at=self.wall_time(), age_seconds=0,
                      reason='; '.join(dict.fromkeys(reasons)) or ('' if total else 'No verified current members'))
        return result

    def _collect(self, store, state, guard, pids, deadline):
        machine, machine_baseline = self._machine()
        result = {'machine_usage': machine, 'jobs': {}, 'external': {}}
        baselines = {}
        for job in state['jobs']:
            if job['status'] not in ACTIVE:
                continue
            if pids is None:
                old = (self.last or {}).get('jobs', {}).get(job['attempt_id'])
                usage = age_usage(old or empty_usage(), self.wall_time())
                usage.update(stale=True, reason='Managed membership snapshot unavailable')
                result['jobs'][job['attempt_id']] = usage
                continue
            try:
                members, reason = observed_group_members(
                    store, job, pids, process_factory=self.process_factory, identity_provider=self.identity)
                usage = self._workload(members, baselines, deadline)
                if reason:
                    usage['reason'] = reason
            except (psutil.Error, OSError, ValueError, JobsError) as exc:
                usage = empty_usage(f'Managed membership unavailable: {type(exc).__name__}')
            result['jobs'][job['attempt_id']] = usage
        for observation in guard['observations']:
            if guard.get('stale'):
                old = (self.last or {}).get('external', {}).get(observation['id'])
                usage = age_usage(old or empty_usage(), self.wall_time())
                usage.update(stale=True, reason='External membership observation stale')
            else:
                usage = self._workload(observation['members'], baselines, deadline)
            result['external'][observation['id']] = usage
        if self.monotonic() > deadline:
            raise TimeoutError('Telemetry sample budget exceeded')
        return result, baselines, machine_baseline
