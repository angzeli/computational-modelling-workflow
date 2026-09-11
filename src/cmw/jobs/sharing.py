"""Bounded primary/auxiliary admission; declarations never confer process ownership."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time

from .store import ACTIVE, PENDING, JobsError, _positive, declared_cpu_demand

WINDOW_AGE = 5.0
LATEST_AGE = 2.0
MIN_INTERVAL = 0.5
CPU_TOLERANCE = 0.05


def scheduling(job):
    return {'role': 'primary', 'allow_auxiliary': False, 'independent': False,
            'resource_contract': None, 'write_scope': job.get('cwd'), **job.get('scheduling', {})}


def scope(path):
    try:
        value = Path(path).expanduser().resolve(strict=True)
        if not value.is_dir():
            raise ValueError('not a directory')
        return value
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise JobsError('Write scope is missing or no longer accessible') from exc


def fingerprint(observation):
    members = observation.get('members', [])
    if not members or len({json.dumps(m['identity'], sort_keys=True) for m in members}) != len(members):
        raise JobsError('External family identity incomplete or duplicated')
    binding = {'id': observation['id'], 'engine': observation['engine'],
               'members': sorted(({k: m.get(k) for k in ('identity', 'exe', 'launcher', 'pgid', 'sid')}
                                  for m in members), key=lambda m: json.dumps(m, sort_keys=True))}
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def guard_fresh(guard, now):
    observed = guard.get('observed_at')
    return (not guard.get('stale') and observed is not None and 0 <= now-observed <= LATEST_AGE
            and guard.get('coverage', {}).get('complete') and guard.get('state') in {'NO_MATCH', 'BUSY'})


def make_reservation(observation, guard, cpus, memory_gib, write_scope=None):
    if not guard_fresh(guard, time.time()) or guard['state'] != 'BUSY':
        raise JobsError('External reservation requires fresh complete BUSY evidence')
    if not any(o['id'] == observation['id'] and fingerprint(o) == fingerprint(observation)
               for o in guard['observations']):
        raise JobsError('External observation identity changed')
    if cpus is None or memory_gib is None:
        raise JobsError('External reservation requires explicit CPU and memory budgets')
    if write_scope is None:
        directories = {m.get('cwd') for m in observation['members']}
        if None in directories or len(directories) != 1:
            raise JobsError('Explicit external write scope required; member working directories incomplete or different')
        write_scope = directories.pop()
    return {'observation_id': observation['id'], 'fingerprint': fingerprint(observation),
            'cpu_budget': _positive(cpus, 'External CPUs', True),
            'memory_gib': _positive(memory_gib, 'External memory'),
            'write_scope': str(scope(write_scope)), 'allow_auxiliary': True,
            'source': 'user supplied', 'created_at': time.time()}


def reservation_state(reservation, guard, now):
    if not reservation:
        return 'absent'
    if not guard_fresh(guard, now):
        return 'uncertain'
    for observation in guard.get('observations', []):
        if observation['id'] == reservation['observation_id']:
            try:
                return 'valid' if fingerprint(observation) == reservation['fingerprint'] else 'expired'
            except (KeyError, JobsError):
                return 'uncertain'
    return 'expired'


def _samples(evidence, now):
    samples = (evidence or {}).get('samples', [])[-2:]
    if len(samples) != 2:
        return [], 'Machine telemetry warming up; two sustained samples required'
    moments = [s.get('observed_at') for s in samples]
    if any(t is None for t in moments) or not (MIN_INTERVAL <= moments[1]-moments[0] <= WINDOW_AGE):
        return [], 'Machine telemetry window incomplete'
    if not (0 <= now-moments[1] <= LATEST_AGE and now-moments[0] <= WINDOW_AGE):
        return [], 'Machine telemetry stale'
    for sample in samples:
        machine = sample.get('machine_usage') or {}
        when = machine.get('observed_at')
        cpu, total, used = (machine.get(k) for k in ('cpu_percent', 'ram_total_bytes', 'ram_used_bytes'))
        count = sample.get('logical_cpus')
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (cpu, total, used, count)) or
                not (0 <= cpu <= 100 and total > 0 and 0 <= used <= total and count > 0 and count == int(count))):
            return [], 'Machine CPU/RAM telemetry invalid'
        if (machine.get('stale') or when is None or not 0 <= now-when <= WINDOW_AGE or
                machine.get('cpu_quality') != 'fresh' or machine.get('memory_quality') != 'fresh' or
                any(machine.get(k) is None for k in ('cpu_percent', 'ram_total_bytes', 'ram_used_bytes')) or
                not sample.get('logical_cpus')):
            return [], 'Machine CPU/RAM telemetry unavailable or stale'
    return samples, ''


def _overruns(active, reservation, valid, evidence, now):
    samples = (evidence or {}).get('samples', [])[-2:]
    workloads = [('jobs', j['attempt_id'], j['display_id'], j['resources'].get('cpus'),
                  j['resources'].get('memory_gib')) for j in active]
    if valid:
        workloads.append(('external', reservation['observation_id'], reservation['observation_id'],
                          reservation['cpu_budget'], reservation['memory_gib']))
    warnings = []
    for section, key, label, cpus, memory in workloads:
        values = [(s.get(section) or {}).get(key) or {} for s in samples]
        current = [v for v in values if v.get('observed_at') is not None and
                   0 <= now-v['observed_at'] <= WINDOW_AGE and not v.get('stale')]
        if memory and current and current[-1].get('rss_bytes') is not None and current[-1]['rss_bytes'] > memory*2**30:
            warnings.append(f'{label}: aggregate RSS exceeds declared memory; shared pages may be counted twice')
        if (cpus and len(current) == 2 and current[1]['observed_at']-current[0]['observed_at'] >= MIN_INTERVAL
                and all(v.get('cpu_quality') in {'fresh', 'partial'} and v.get('cpu_cores') is not None
                        and math.isfinite(v['cpu_cores']) and v['cpu_cores'] > cpus+CPU_TOLERANCE for v in current)):
            warnings.append(f'{label}: sustained observed CPU exceeds declared CPU budget')
    return warnings


def evaluate(state, guard, evidence=None, *, candidate_id=None, final=False, now=None):
    """Shared decision over a fresh snapshot; Starting is the atomic commitment.

For final GO the claimed candidate is temporarily removed from active totals,
then charged once through the same candidate admission calculation.
"""
    now = time.time() if now is None else now
    policy = state.get('scheduler', state.get('controller', {}).get('scheduler', {}))
    bounded = policy.get('mode', 'sequential') == 'bounded-sharing'
    jobs = state['jobs']
    active_all = [j for j in jobs if j['status'] in ACTIVE]
    candidate = next((j for j in jobs if j['id'] == candidate_id), None) if candidate_id is not None else None
    active = [j for j in active_all if not (final and candidate and j['id'] == candidate['id'])]
    pending = sorted((j for j in jobs if j['status'] in PENDING), key=lambda j: j['order'])
    if final and candidate:
        virtual = {**candidate, 'status': 'Queue', 'order': candidate.get('admission_order', 0)}
        pending = sorted([virtual, *pending], key=lambda j: j['order'])
    primary = [j for j in active if scheduling(j)['role'] == 'primary']
    auxiliary = [j for j in active if scheduling(j)['role'] == 'auxiliary']
    reservation = policy.get('external_reservation')
    validity = reservation_state(reservation, guard, now)
    valid = validity == 'valid' and reservation.get('allow_auxiliary')
    observations = guard.get('observations', [])
    cpu = sum(j['resources'].get('cpus') or 0 for j in active_all)
    memory = sum(j['resources'].get('memory_gib') or 0 for j in active_all)
    if bounded and valid:
        cpu += reservation['cpu_budget']; memory += reservation['memory_gib']
    if any(j['resources'].get('cpus') is None for j in active_all):
        cpu = None
    if any(j['resources'].get('memory_gib') is None for j in active_all):
        memory = None
    anchor_summary = None
    if bounded:
        if auxiliary:
            anchor_summary = auxiliary[0].get('sharing_anchor')
        elif len(primary) == 1:
            anchor_summary = {'kind': 'managed', 'attempt_id': primary[0]['attempt_id'], 'id': primary[0]['display_id']}
        elif valid and len(observations) == 1:
            anchor_summary = {'kind': 'external', 'fingerprint': reservation['fingerprint'], 'id': reservation['observation_id']}
    decision = dict(permitted=False, reason='', mode='Bounded Sharing' if bounded else 'Sequential',
                    candidate_id=None, anchor_primary=anchor_summary, cpu_committed=cpu,
                    memory_committed_gib=memory, cpu_budget=policy.get('cpu_budget'),
                    memory_budget_gib=policy.get('memory_gib'), host_cpu_headroom=None,
                    host_memory_available_bytes=None, write_scope_result='not evaluated',
                    external_reservation_state=validity, auxiliary_slot='occupied' if auxiliary else 'free',
                    warnings=_overruns(active_all, reservation, valid, evidence, now) if bounded else [],
                    authority='Client projection; controller and supervisor recheck',
                    conflict=bool(active and observations and not (bounded and valid and not primary)))
    def refuse(reason):
        decision['reason'] = reason
        return decision
    # Select the lane before reporting global vetoes, so queued reasons stay concrete.
    sharing_window = bounded and bool(primary or observations)
    lane = [j for j in pending if scheduling(j)['role'] == 'auxiliary'] if sharing_window else pending
    selected = lane[0] if lane else None
    if candidate is None:
        candidate = selected
    if candidate:
        decision['candidate_id'] = candidate['id']
    control = state['controller']
    if not control.get('online', False):
        return refuse('Controller offline')
    if control.get('stale'):
        return refuse('Controller stale')
    if not control.get('dispatch') or control.get('stop'):
        return refuse(control.get('reason') or 'Dispatch intent paused')
    if any(j['status'] in {'Unknown', 'Cancelling'} for j in active_all):
        return refuse('Unknown or Cancelling attempt blocks new admission')
    if not guard_fresh(guard, now):
        return refuse('External observation stale, unavailable or uncertain')
    if not bounded:
        if observations or guard['state'] != 'NO_MATCH':
            return refuse(guard['reason'])
        if active:
            return refuse('Managed execution occupies slot')
    else:
        if decision['warnings']:
            return refuse(decision['warnings'][0])
        if len(primary) > 1 or len(auxiliary) > 1:
            return refuse('Bounded Sharing active-role invariant violated')
        if auxiliary:
            return refuse('Auxiliary slot already occupied; next Primary waits until it finishes')
        if observations:
            if primary or len(observations) != 1:
                return refuse('Additional external computation blocks admission')
            if not valid:
                return refuse('External reservation identity expired' if validity == 'expired' else
                              'External workload has no valid coexistence reservation')
    if candidate is None:
        return refuse('No pending Auxiliary' if sharing_window else 'No pending job')
    if sharing_window and scheduling(candidate)['role'] == 'primary':
        return refuse('Primary jobs remain sequential; waiting for the active sharing window')
    if selected is None or selected['id'] != candidate['id']:
        return refuse('Queue lane order changed')
    if selected['status'] == 'Hold':
        return refuse('User hold at Auxiliary head' if sharing_window else 'User hold')
    if not final and candidate['status'] != 'Queue':
        return refuse('Candidate is no longer queued')
    if sharing_window:
        declaration = scheduling(candidate)
        if declaration['role'] != 'auxiliary':
            return refuse('Primary jobs remain sequential')
        if primary:
            anchor = primary[0]
            decision['anchor_primary'] = {'kind': 'managed', 'attempt_id': anchor['attempt_id'], 'id': anchor['display_id']}
            if anchor['status'] != 'Run':
                return refuse('Primary must be running before Auxiliary admission')
            if not scheduling(anchor)['allow_auxiliary']:
                return refuse('Active primary does not allow auxiliary coexistence')
            anchor_scope = scheduling(anchor)['write_scope']
        else:
            decision['anchor_primary'] = {'kind': 'external', 'fingerprint': reservation['fingerprint'], 'id': reservation['observation_id']}
            anchor_scope = reservation['write_scope']
        if not declaration['independent']:
            return refuse('Auxiliary independence assertion missing')
        if declaration['resource_contract'] != 'trusted-declared':
            return refuse('Auxiliary resource contract missing')
        cpus, ram = candidate['resources'].get('cpus'), candidate['resources'].get('memory_gib')
        if not cpus or not ram:
            return refuse('Auxiliary requires explicit CPU and memory requests')
        if any(not j['resources'].get('cpus') or not j['resources'].get('memory_gib') for j in active):
            return refuse('Active primary lacks explicit CPU or memory commitment')
        for item in [*active, candidate]:
            if declared_cpu_demand(item['resources']) > item['resources']['cpus']:
                return refuse(f"Declared MPI/thread demand exceeds CPU request for {item['display_id']}")
        committed_cpu = sum(j['resources']['cpus'] for j in active)
        committed_ram = sum(j['resources']['memory_gib'] for j in active)
        if valid:
            committed_cpu += reservation['cpu_budget']; committed_ram += reservation['memory_gib']
        if any(policy.get(k) is None for k in ('cpu_budget', 'memory_gib', 'cpu_reserve', 'min_available_gib')):
            return refuse('Bounded Sharing resource policy is not configured')
        if committed_cpu+cpus > policy['cpu_budget']:
            return refuse(f"CPU commitment: {committed_cpu} + {cpus} > {policy['cpu_budget']}")
        if committed_ram+ram > policy['memory_gib']:
            return refuse(f"Memory commitment: {committed_ram:g} + {ram:g} > {policy['memory_gib']:g} GiB")
        try:
            left, right = scope(anchor_scope), scope(declaration['write_scope'])
            if left == right or left in right.parents or right in left.parents:
                decision['write_scope_result'] = 'overlap'
                return refuse('Write scope overlaps active primary')
            decision['write_scope_result'] = 'disjoint'
        except JobsError as exc:
            decision['write_scope_result'] = 'unavailable'
            return refuse(str(exc))
        samples, reason = _samples(evidence, now)
        if reason:
            return refuse(reason)
        swap = [s['machine_usage'].get('swap_out_bytes') for s in samples]
        if all(isinstance(v, int) and v >= 0 for v in swap) and swap[1] > swap[0]:
            return refuse('Recent swap-out activity vetoes new Auxiliary admission')
        free = min(s['logical_cpus']*(1-s['machine_usage']['cpu_percent']/100) for s in samples)
        available = min(s['machine_usage']['ram_total_bytes']-s['machine_usage']['ram_used_bytes'] for s in samples)
        decision.update(host_cpu_headroom=free, host_memory_available_bytes=available)
        if free < cpus+policy['cpu_reserve']:
            return refuse(f"Host CPU headroom {free:.2f} cores < {cpus} CPU request + {policy['cpu_reserve']:g} core reserve")
        if available < (ram+policy['min_available_gib'])*2**30:
            return refuse(f"Available RAM {available/2**30:.2f} GiB < {ram:g} GiB request + {policy['min_available_gib']:g} GiB safety margin")
    elif active:
        return refuse('Active execution blocks ordinary sequential admission')
    if final and candidate.get('sharing_anchor') != decision['anchor_primary']:
        return refuse('Anchor primary changed before payload GO')
    from .runtime import launch_blocker
    blocker = launch_blocker(candidate)
    if blocker:
        return refuse(blocker)
    decision.update(permitted=True, reason='Auxiliary coexistence permitted' if sharing_window else 'Sequential candidate permitted')
    return decision


class EvidenceWindow:
    """Two recent ephemeral samples, independent for controller and each viewer."""
    def __init__(self):
        self.samples = []

    def add(self, usage, *, source):
        import psutil
        now = time.time()
        sample = {**usage, 'observed_at': now, 'logical_cpus': psutil.cpu_count(logical=True)}
        if not self.samples or now-self.samples[-1]['observed_at'] >= MIN_INTERVAL:
            self.samples = [*self.samples[-1:], sample]
        return {'samples': self.samples, 'source': source}


class AdmissionSampler:
    """Controller/supervisor evidence never comes from a watch client's cache."""
    def __init__(self):
        from .telemetry import Sampler
        import psutil
        self.sampler = Sampler(swap_memory=psutil.swap_memory)
        self.window = EvidenceWindow()

    def collect(self, store, *, source='controller', wait=False):
        from . import activity
        end = time.monotonic()+2.5 if wait else 0
        while True:
            state = store.snapshot()
            state['_telemetry_membership'] = True
            guard = activity.DEFAULT_OBSERVER.scan(store, state, source=source)
            pids = guard.pop('_pids', None)
            if guard.get('stale'):
                pids = None
            usage = self.sampler.sample(store, state, guard, pids)
            evidence = self.window.add(usage, source=source)
            # One baseline + two intervals. Unavailable data remains a veto.
            if not wait or _samples(evidence, time.time())[0] or time.monotonic() >= end:
                return guard, evidence
            time.sleep(MIN_INTERVAL + 0.05)
