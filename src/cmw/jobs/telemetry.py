"""Ephemeral observed usage of identities already verified by Jobs.

Fresh host samples may veto Bounded Sharing admission. They never grant ownership,
certify completion, or release commitments because usage is low. Requested,
committed and observed resources remain distinct; this module enforces no limits.
CPU baselines belong to one sampler and use monotonic elapsed time.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import threading
import time

import psutil

from .ownership import identity, observed_group_members
from .native_memory import footprint_reader
from .thermal import ThermalProvider, age_thermal, unavailable_thermal
from .store import ACTIVE, JobsError

MAX_AGE = 5.0
SAMPLE_BUDGET = 0.75
_AUTO = object()
RSS_SOURCE = 'psutil.Process.memory_info().rss'
FOOTPRINT_SOURCE = 'proc_pid_rusage(RUSAGE_INFO_V0).ri_phys_footprint'


def _bytes(value):
    """Reject invalid counters rather than manufacturing measured zero."""
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value < 0 or int(value) != value):
        raise ValueError('Invalid byte counter')
    return int(value)


def empty_usage(reason='No verified current members'):
    return dict(cpu_cores=None, rss_bytes=None, cpu_quality='unavailable',
                memory_quality='unavailable', cpu_members=0, memory_members=0,
                total_members=0, observed_members=0, cpu_sample_members=0,
                scope=None, memory_source=RSS_SOURCE,
                memory_semantics='sum of resident sets; shared pages may be counted more than once',
                expected_vasp_ranks=None, observed_vasp_ranks=None,
                coverage_quality='unavailable', coverage_reason=reason,
                footprint_bytes=None, footprint_members=0, footprint_quality='unavailable',
                footprint_source=None, footprint_reason='Physical footprint unavailable',
                process_samples=[], observed_at=None, age_seconds=None, stale=False, reason=reason)


def age_usage(value, now=None):
    """A pure presentation projection; never reads the operating system."""
    result = copy.deepcopy(value)
    if result and result.get('observed_at') is not None:
        result['age_seconds'] = max(0, (time.time() if now is None else now)-result['observed_at'])
        result['stale'] = bool(result.get('stale') or result['age_seconds'] > MAX_AGE)
    if result and 'thermal' in result:
        result['thermal'] = age_thermal(result['thermal'], now=now)
    return result


class Sampler:
    """At most one in-flight read, with atomic acceptance of samples/baselines.

A late read after a timeout cannot overwrite the accepted client snapshot or
advance its CPU baseline. Access failures leave guard evidence untouched.
"""
    def __init__(self, *, process_factory=psutil.Process, identity_provider=identity,
                 cpu_times=psutil.cpu_times, virtual_memory=psutil.virtual_memory,
                 monotonic=time.monotonic, wall_time=time.time, swap_memory=psutil.swap_memory,
                 footprint=_AUTO, thermal=_AUTO):
        self.process_factory = process_factory
        self.identity = identity_provider
        self.cpu_times = cpu_times
        self.virtual_memory = virtual_memory
        self.swap_memory = swap_memory
        self.footprint = footprint_reader() if footprint is _AUTO else footprint
        self.thermal = ThermalProvider.from_environment() if thermal is _AUTO else thermal
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.baselines = {}
        self.machine_baseline = None
        self.last = None
        self.thread = None
        self.lock = threading.Lock()

    def close(self):
        if self.thermal is not None:
            try:
                self.thermal.close()
            except Exception:
                pass

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
                    ram_available_bytes=None, memory_source='psutil.virtual_memory',
                    memory_semantics='total minus available; available is platform-defined reclaimable memory',
                    memory_categories_bytes={}, swap_used_bytes=None, swap_total_bytes=None,
                    swap_out_bytes=None, swap_source='psutil.swap_memory',
                    observed_at=None, age_seconds=None, stale=False, reason='')

    def _machine(self):
        result, baseline = self._empty_machine(), None
        try:
            result['thermal'] = (self.thermal.snapshot() if self.thermal is not None else
                                 unavailable_thermal('CPU temperature observation disabled'))
        except Exception:
            result['thermal'] = unavailable_thermal('CPU temperature provider failed')
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
            total, available = _bytes(memory.total), _bytes(memory.available)
            if total <= 0 or available > total:
                raise ValueError('Invalid available memory')
            categories = {}
            for field in ('available', 'used', 'free', 'active', 'inactive', 'wired', 'cached', 'compressed'):
                try:
                    categories[field] = _bytes(getattr(memory, field, None))
                except (ValueError, TypeError, OverflowError):
                    categories[field] = None
            # Preserve the existing admission formula. psutil.used has different
            # platform semantics; neither process sums nor swap belong here.
            result.update(ram_used_bytes=total-available, ram_total_bytes=total,
                          ram_available_bytes=available, memory_categories_bytes=categories,
                          memory_quality='fresh')
        except (psutil.Error, OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            reasons.append(f'Machine memory: {type(exc).__name__}')
        if self.swap_memory is not None:
            try:
                swap = self.swap_memory()
                for field, target in (('used', 'swap_used_bytes'), ('total', 'swap_total_bytes'),
                                      ('sout', 'swap_out_bytes')):
                    try:
                        result[target] = _bytes(getattr(swap, field, None))
                    except (ValueError, TypeError, OverflowError):
                        result[target] = None
            except (psutil.Error, OSError, ValueError, TypeError, AttributeError) as exc:
                reasons.append(f'Machine swap: {type(exc).__name__}')
        result.update(observed_at=self.wall_time(), age_seconds=0, reason='; '.join(reasons))
        return result, baseline

    def _workload(self, members, baseline_out, deadline, *, job=None):
        from .activity import LAUNCHERS, NAMES, native_executable

        result = empty_usage()
        # One process may be reachable through several verified links. Its birth
        # identity, not the number of links, is the unit of accounting.
        unique = {json.dumps(m['identity'], sort_keys=True): m for m in members}
        result['total_members'] = len(unique)
        result['scope'] = ('verified external members' if job is None else
                           'verified payload session' if job.get('env', {}).get('CMW_JOBS_OWN_SESSION') == '1'
                           else 'verified process group')
        expected = (job or {}).get('resources', {}).get('mpi_ranks')
        vasp = ((job or {}).get('engine') or '').upper() == 'VASP'
        if vasp and isinstance(expected, int) and not isinstance(expected, bool) and expected > 0:
            result['expected_vasp_ranks'] = expected
        result['footprint_source'] = FOOTPRINT_SOURCE if self.footprint is not None else None
        rates, memory, footprints, new, cpu_samples = [], [], [], 0, 0
        reasons, footprint_reasons = [], []
        for key, member in unique.items():
            if self.monotonic() > deadline:
                raise TimeoutError('Telemetry sample budget exceeded')
            owner = member['identity']
            try:
                p = self.process_factory(owner['pid'])
                if self.identity(owner['pid']) != owner or p.status() == psutil.STATUS_ZOMBIE:
                    reasons.append('Member identity changed or became zombie')
                    continue
                cpu, rss, footprint = None, None, None
                try:
                    times = p.cpu_times()
                    cpu = float(times.user + times.system)
                    if not math.isfinite(cpu) or cpu < 0:
                        raise ValueError('Invalid CPU counter')
                except (psutil.Error, OSError, ValueError, TypeError, AttributeError):
                    cpu = None
                    reasons.append('Member CPU inaccessible or invalid')
                try:
                    rss = _bytes(p.memory_info().rss)
                except (psutil.Error, OSError, ValueError, TypeError, AttributeError, OverflowError):
                    reasons.append('Member RSS inaccessible or invalid')
                if self.footprint is not None:
                    try:
                        footprint = _bytes(self.footprint(owner['pid']))
                    except (psutil.Error, OSError, ValueError, TypeError, AttributeError, OverflowError):
                        footprint_reasons.append('Member physical footprint inaccessible or invalid')
                # Executables classify already attributable members only. They
                # never discover or adopt ownership by engine name.
                try:
                    exe = native_executable(p)
                    if not isinstance(exe, str) or not exe:
                        exe = None
                except (psutil.Error, OSError, AttributeError):
                    exe = None
                try:
                    ppid = p.ppid()
                    if not isinstance(ppid, int):
                        ppid = None
                except (psutil.Error, OSError, AttributeError):
                    ppid = None
                moment = self.monotonic()
                if self.identity(owner['pid']) != owner or p.status() == psutil.STATUS_ZOMBIE:
                    reasons.append('Member identity changed during usage read')
                    continue
                if member.get('pgid') is not None and os.getpgid(owner['pid']) != member['pgid']:
                    reasons.append('Member left verified process group')
                    continue
                basename = Path(exe).name if exe else None
                role = ('VASP rank' if NAMES.get(basename) == 'VASP' else
                        'MPI launcher' if basename in LAUNCHERS else
                        'worker' if job and owner == job.get('worker') else
                        'anchor' if job and owner == job.get('group') else 'payload process')
                result['process_samples'].append(dict(identity=dict(owner), pgid=member.get('pgid'),
                    ppid=ppid, exe=exe, role=role, cpu_seconds=cpu, rss_bytes=rss,
                    footprint_bytes=footprint, observed_at=self.wall_time()))
                if rss is not None:
                    memory.append(rss)
                if footprint is not None:
                    footprints.append(footprint)
                if cpu is not None:
                    cpu_samples += 1
                    baseline_out[key] = (cpu, moment)
                    before = self.baselines.get(key)
                    if before and 0 < moment-before[1] <= MAX_AGE and cpu >= before[0]:
                        rates.append((cpu-before[0])/(moment-before[1]))
                    else:
                        new += 1
            except (psutil.NoSuchProcess, ProcessLookupError):
                reasons.append('Member exited during usage observation')
            except (psutil.Error, OSError) as exc:
                reasons.append(f'Member usage unavailable: {type(exc).__name__}')
        total = result['total_members']
        samples = result['process_samples']
        coverage_reasons = []
        if len(samples) < total:
            coverage_reasons.append(f'{len(samples)}/{total} nominated identities observable')
        if vasp:
            result['observed_vasp_ranks'] = (sum(p['role'] == 'VASP rank' for p in samples)
                                             if any(p['exe'] for p in samples) else None)
            if any(p['exe'] is None for p in samples):
                coverage_reasons.append('VASP rank executable metadata incomplete')
            expected = result['expected_vasp_ranks']
            observed = result['observed_vasp_ranks']
            if expected is not None and observed != expected:
                coverage_reasons.append(f'{observed if observed is not None else "unknown"}/{expected} VASP ranks observed')
        result.update(observed_members=len(samples), cpu_sample_members=cpu_samples,
                      coverage_quality='partial' if coverage_reasons else 'complete' if samples else 'unavailable',
                      coverage_reason='; '.join(coverage_reasons),
                      cpu_cores=sum(rates) if rates else None,
                      rss_bytes=sum(memory) if memory else None,
                      footprint_bytes=sum(footprints) if footprints else None,
                      cpu_members=len(rates), memory_members=len(memory), footprint_members=len(footprints))
        complete = not coverage_reasons
        result['cpu_quality'] = ('fresh' if rates and len(rates) == total and complete else 'partial' if rates else
                                 'warming-up' if new and new == total else 'unavailable')
        for field, values in (('memory_quality', memory), ('footprint_quality', footprints)):
            result[field] = 'fresh' if values and len(values) == total and complete else 'partial' if values else 'unavailable'
        result['footprint_reason'] = ('Physical footprint unavailable on this platform/provider' if self.footprint is None
                                      else '; '.join(dict.fromkeys(footprint_reasons)))
        if new:
            reasons.append(f'{new} CPU baseline(s) warming up')
        result.update(observed_at=self.wall_time(), age_seconds=0,
                      reason='; '.join(dict.fromkeys(reasons + coverage_reasons)) or ('' if total else 'No verified current members'))
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
                usage = self._workload(members, baselines, deadline, job=job)
                if reason:
                    usage['reason'] = '; '.join(filter(None, (reason, usage['reason'])))
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
