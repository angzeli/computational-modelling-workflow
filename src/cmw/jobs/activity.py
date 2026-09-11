"""Read-only current-user executable observations; never execution ownership.

Scanning cannot reserve the machine against a subsequent manual launch. Each
admission scan uses new Process objects, not process_iter's shared cache.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import psutil

from .ownership import identity, lock_held, owner_alive
from .store import ACTIVE, PENDING

NAMES = {'vasp_std': 'VASP', 'vasp_gam': 'VASP', 'vasp_ncl': 'VASP', 'orca': 'ORCA'}
LAUNCHERS = {'prte', 'prterun', 'mpirun', 'orterun', 'orted', 'prted'}
SCAN_BUDGET = 1.0
MAX_AGE = 2.0
SCOPE = 'Current local user; recognized executables; accessible process metadata'


def native_executable(process):
    # psutil 7.2.2 Process.exe() can guess from argv[0] after an OS denial.
    # The platform accessor avoids both that guess and the per-object exe cache.
    return process._proc.exe()


def executable_paths(store):
    """Small additive exact-path mapping; no executable/configuration hooks."""
    path = store.root / 'external-activity.json'
    if not path.exists():
        return {}
    if path.stat().st_size > 16384:
        raise ValueError('external-activity.json exceeds 16 KiB')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {'executables'} or not isinstance(data['executables'], dict):
        raise ValueError('external-activity.json requires an executables path-to-engine mapping')
    result = {}
    for path, engine in data['executables'].items():
        if not Path(path).is_absolute() or engine not in {'VASP', 'ORCA'}:
            raise ValueError('External executable mappings require absolute paths and VASP or ORCA')
        result[str(Path(path).resolve())] = engine
    return result


def recognition(exe, name, paths):
    if exe:
        if exe in paths:
            return paths[exe], 'Configured exact OS executable path'
        engine = NAMES.get(Path(exe).name)
        return (engine, 'OS executable basename match') if engine else (None, None)
    engine = NAMES.get(name)
    if not engine:
        engine = next((engine for path, engine in paths.items() if Path(path).name == name), None)
    return (engine, 'Recognized process name; OS executable unavailable') if engine else (None, None)


def unavailable(reason, *, previous=None, source='client'):
    now = time.time()
    result = copy.deepcopy(previous) if previous else {'observations': [], 'observed_at': None, 'coverage': {}}
    result.update(state='UNAVAILABLE', reason=reason, source=source, scope=SCOPE, stale=True,
                  checked_at=now, age_seconds=None if result.get('observed_at') is None else max(0, now-result['observed_at']))
    result['coverage'] = {**result.get('coverage', {}), 'complete': False, 'warnings': [reason]}
    return result


def permits(result):
    now = time.time()
    observed = result.get('observed_at')
    return bool(result.get('state') == 'NO_MATCH' and not result.get('stale') and
                observed is not None and 0 <= now-observed <= MAX_AGE and result.get('coverage', {}).get('complete'))


def collect(store, state, deadline, *, pids_provider=psutil.pids, process_factory=psutil.Process):
    paths = executable_paths(store)
    coverage = {'complete': True, 'scanned': 0, 'out_of_scope': 0, 'vanished': 0, 'zombies': 0, 'managed': 0, 'warnings': []}
    candidates, uncertain, limitations = [], [], []
    uid = os.getuid()
    def budget():
        if time.monotonic() > deadline:
            raise TimeoutError('External observation exceeded its bounded scan budget')
    def fresh(pid):
        budget()
        p = process_factory(pid)
        return p, identity(pid)
    def launcher_family(c):
        """Verify one direct MPI parent, optionally beneath our payload guardian.

        The bounded ancestry is grouping evidence only; it grants no ownership.
        Repeating this at final collection also binds intermediate wrapper births.
        """
        parent, parent_id = fresh(c['ppid'])
        parent_exe = native_executable(parent)
        sid = c['sid']
        if (Path(parent_exe).name not in LAUNCHERS or parent.uids().real != uid or
                os.getsid(parent.pid) != sid or identity(parent.pid) != parent_id or
                native_executable(process_factory(parent.pid)) != parent_exe):
            return None
        binding = {'launcher': parent_id, 'launcher_exe': parent_exe, 'family': None}
        if parent.pid == sid:
            return binding  # Existing launcher-as-session-leader contract.
        ancestry, seen = [], set()
        current = parent.pid
        for _ in range(8):
            if current in seen:
                return None
            seen.add(current)
            node, node_id = fresh(current)
            exe = native_executable(node)
            ppid, pgid = node.ppid(), os.getpgid(current)
            if (not exe or node.uids().real != uid or node.status() == psutil.STATUS_ZOMBIE or
                    os.getsid(current) != sid or node.terminal() is not None):
                return None
            ancestry.append({'identity': node_id, 'exe': exe, 'ppid': ppid, 'pgid': pgid, 'sid': sid})
            if current == sid:
                argv = node.cmdline()
                payload = str(Path(__file__).with_name('payload.sh').resolve())
                if (exe != '/bin/bash' or pgid != sid or len(argv) < 2 or
                        not Path(argv[1]).is_absolute() or str(Path(argv[1]).resolve()) != payload):
                    return None
                binding['family'] = {'session_leader': node_id, 'ancestry': ancestry, 'payload': payload}
                # The first node must still be the initially verified launcher.
                return binding if ancestry[0]['identity'] == parent_id and ancestry[0]['exe'] == parent_exe else None
            current = ppid
        return None
    pids = list(pids_provider())
    for pid in pids:
        budget()
        try:
            p = process_factory(pid)
            if p.uids().real != uid:
                coverage['out_of_scope'] += 1
                continue
            coverage['scanned'] += 1
            if p.status() == psutil.STATUS_ZOMBIE:
                coverage['zombies'] += 1
                continue
            name = p.name()
            try:
                exe = native_executable(p)
            except psutil.AccessDenied:
                exe = ''
            engine, evidence = recognition(exe, name, paths)
            if not exe and not engine:
                limitations.append(f'PID {pid} ({name}): executable unavailable; no recognized engine name (non-blocking)')
                continue
            if not engine:
                continue
            try:
                owner = identity(pid)
                status = p.status()
                if status == psutil.STATUS_ZOMBIE:
                    coverage['zombies'] += 1
                    continue
                ppid, pgid, sid = p.ppid(), os.getpgid(pid), os.getsid(pid)
                q, again = fresh(pid)
                exe_again = native_executable(q)
                if owner != again or exe_again != exe or q.uids().real != uid:
                    uncertain.append(f'PID {pid}: identity or executable changed during observation')
                    continue
            except (psutil.AccessDenied, PermissionError):
                uncertain.append(f'PID {pid}: recognized {engine} identity cannot be verified')
                continue
            missing = []
            try:
                cwd = q.cwd() or None
            except psutil.AccessDenied:
                cwd = None
                missing.append('Working directory inaccessible (optional)')
            candidate = {'identity': owner, 'pid': pid, 'engine': engine, 'exe': exe or None,
                         'status': status, 'ppid': ppid, 'pgid': pgid, 'sid': sid, 'cwd': cwd,
                         'evidence': [evidence], 'missing': missing, 'ownership': 'External / unattributed'}
            if not exe:
                uncertain.append(f'PID {pid}: recognized {engine} executable cannot be verified')
            # Exemption requires the persisted live guardian lock and pinned group
            # identity, then independently observed group/session membership.
            managed = False
            for job in state.get('jobs', []):
                if job['status'] not in ACTIVE or not job.get('group'):
                    continue
                group = job['group']
                session = job.get('env', {}).get('CMW_JOBS_OWN_SESSION') == '1'
                if (not session and pgid != group['pid']) or sid != group['pid']:
                    continue
                try:
                    verified = (owner_alive(group) and owner_alive(job.get('worker')) and
                                lock_held(store.root/'attempts'/job['attempt_id']/'worker.lock', strict=True))
                except OSError:
                    verified = False
                if verified:
                    # Membership alone is used only while the original pinned
                    # group leader is birth-verified, never for a bare PGID.
                    if (identity(pid) == owner and os.getsid(pid) == group['pid']
                            and (session or os.getpgid(pid) == group['pid'])):
                        managed = True
                else:
                    uncertain.append(f'PID {pid}: selected queue group ownership unresolved')
            if managed:
                coverage['managed'] += 1
                continue
            # Same session/cwd alone never groups engines. Nested launchers need
            # a verified ancestry ending at this runtime's dedicated payload shell.
            candidate['launcher'] = None
            candidate['launcher_exe'] = None
            candidate['family'] = None
            try:
                binding = launcher_family(candidate)
                if binding:
                    candidate.update(binding)
                    candidate['evidence'].append('Direct verified MPI launcher parent and verified dedicated session ancestry (grouping inference)')
            except (psutil.Error, OSError):
                pass  # Grouping failure never removes an independently live engine.
            candidates.append(candidate)
        except (psutil.NoSuchProcess, ProcessLookupError):
            coverage['vanished'] += 1
        except (psutil.AccessDenied, PermissionError):
            # UID itself unreadable means scope cannot be established. Verified
            # other-user processes were excluded before sensitive metadata reads.
            uncertain.append(f'PID {pid}: intended user scope or critical metadata unavailable')
    budget()
    # Revalidate every surviving candidate at the end, including exec transitions
    # and launcher/session ancestry loss. Sharing consumes only this fresh result.
    live = []
    for c in candidates:
        try:
            p, owner = fresh(c['pid'])
            if owner != c['identity'] or native_executable(p) != c['exe']:
                uncertain.append(f"PID {c['pid']}: final identity/executable revalidation failed")
                continue
            if p.status() == psutil.STATUS_ZOMBIE:
                coverage['zombies'] += 1
                continue
            if (p.ppid(), os.getpgid(c['pid']), os.getsid(c['pid'])) != (c['ppid'], c['pgid'], c['sid']):
                uncertain.append(f"PID {c['pid']}: final parent/group/session revalidation failed")
                continue
            if c['launcher']:
                try:
                    if launcher_family(c) != {k: c[k] for k in ('launcher', 'launcher_exe', 'family')}:
                        c['launcher'] = None
                except (psutil.Error, OSError):
                    c['launcher'] = None
                if c['launcher'] is None:
                    c['evidence'] = [e for e in c['evidence'] if 'grouping inference' not in e]
                    c['launcher_exe'] = None
                    c['family'] = None
            live.append(c)
        except (psutil.NoSuchProcess, ProcessLookupError):
            coverage['vanished'] += 1
        except (psutil.AccessDenied, PermissionError):
            uncertain.append(f"PID {c['pid']}: final identity inaccessible")
    now = time.time()
    grouped = {}
    for c in live:
        owner = c['launcher'] or c['identity']
        key = json.dumps(owner, sort_keys=True) + c['engine']
        if c['launcher']:
            # Homogeneous executable families, never all engines under a shell.
            key += json.dumps({'exe': c['exe'], 'family': c['family']}, sort_keys=True)
        grouped.setdefault(key, []).append(c)
    observations = []
    for key, members in grouped.items():
        first = min(members, key=lambda c: (c['identity']['birth'], c['pid']))
        observations.append({**first, 'id': 'E'+hashlib.sha256(key.encode()).hexdigest()[:12],
                             'nproc': len(members), 'members': members,
                             'age_seconds': max(0, now-first['identity']['birth']),
                             'observed_at': now, 'cpus': None, 'memory_gib': None})
    coverage['limitations'] = limitations[:20]
    coverage['warnings'] = uncertain[:20] + limitations[:20]
    coverage['uncertainties'] = len(uncertain)
    coverage['complete'] = not uncertain
    state_name = 'BUSY' if observations else 'UNCERTAIN' if uncertain else 'NO_MATCH'
    reason = ('Recognized external computation observed' if observations else
              'Material process observation uncertainty' if uncertain else 'No recognized external computation observed')
    result = {'state': state_name, 'reason': reason, 'scope': SCOPE, 'observed_at': now,
            'checked_at': now, 'age_seconds': 0, 'stale': False, 'coverage': coverage,
            'observations': observations}
    if state.get('_telemetry_membership'):
        result['_pids'] = list(pids)
    return result


class Observer:
    """One bounded in-flight collection per client/controller; no shared cache reset.

An unresponsive OS read leaves at most one daemon collector, never unbounded
threads. Its late result cannot authorize admission; a later call starts fresh.
"""
    def __init__(self, collector=collect):
        self.collector = collector
        self.thread = None
        self.last = None
        self.lock = threading.Lock()

    def scan(self, store, state=None, *, source='client'):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return unavailable('Previous external observation still unavailable', previous=self.last, source=source)
            box = {}
            def run():
                try:
                    box['result'] = self.collector(store, state if state is not None else store.snapshot(), time.monotonic()+SCAN_BUDGET)
                except Exception as exc:
                    box['error'] = f'External observation unavailable: {type(exc).__name__}: {exc}'
            self.thread = threading.Thread(target=run, daemon=True, name='cmw-external-observation')
            self.thread.start()
            self.thread.join(SCAN_BUDGET)
            if self.thread.is_alive() or 'error' in box:
                return unavailable(box.get('error', 'External observation timed out'), previous=self.last, source=source)
            result = box['result']
            if result.get('state') == 'NO_MATCH' and not permits(result):
                return unavailable('External clear observation is stale or incomplete', previous=result, source=source)
            result['source'] = source
            self.last = copy.deepcopy(result)
            self.last.pop('_pids', None)
            return result


DEFAULT_OBSERVER = Observer()


def admission(state, guard):
    """Project effective permission without changing dispatch intent or queue state."""
    if state.get('scheduler', {}).get('mode') == 'bounded-sharing':
        from .sharing import evaluate
        return evaluate(state, guard, state.get('sharing_evidence'))
    control = state['controller']
    active = any(j['status'] in ACTIVE for j in state['jobs'])
    pending = sorted((j for j in state['jobs'] if j['status'] in PENDING), key=lambda j:j['order'])
    allowed = permits(guard)
    reason = (guard['reason'] if not allowed else 'Controller offline' if not control['online'] else
              'Controller stale' if control['stale'] else 'Dispatch intent paused' if not control['dispatch'] else
              'Managed execution occupies slot' if active else 'No pending job' if not pending else
              'User hold' if pending[0]['status']=='Hold' else 'Guard permits remaining queue/preflight checks')
    ready = bool(allowed and control['online'] and not control['stale'] and control['dispatch'] and not active and pending and pending[0]['status']=='Queue')
    if ready:
        from .runtime import launch_blocker
        blocker = launch_blocker(pending[0])
        if blocker:
            ready, reason = False, blocker
    return {'permitted': ready,
            'reason': reason, 'conflict': bool(active and guard['observations']),
            'authority': 'Client observation only; controller rechecks before launch'}


def project(store, state=None, observer=None, sampler=None):
    state = copy.deepcopy(state if state is not None else store.snapshot())
    state['_telemetry_membership'] = True
    guard = (observer or DEFAULT_OBSERVER).scan(store, state, source='client')
    state.pop('_telemetry_membership', None)
    pids = guard.pop('_pids', None)
    if guard.get('stale'):
        pids = None
    control = state['controller']
    state['external_activity'] = guard
    state['controller_guard'] = control.get('external_guard')
    state['admission'] = admission(state, guard)
    pending = sorted((j for j in state['jobs'] if j['status'] in PENDING), key=lambda j:j['order'])
    if not permits(guard) and pending and pending[0]['status']=='Queue' and control['dispatch'] and not any(j['status'] in ACTIVE for j in state['jobs']):
        pending[0]['reason'] = guard['reason']
    from .telemetry import Sampler
    sampler = sampler or Sampler()
    if state.get('scheduler', {}).get('mode') == 'bounded-sharing':
        sampler.swap_memory = psutil.swap_memory
    usage = sampler.sample(store, state, guard, pids)
    state['machine_usage'] = usage['machine_usage']
    for job in state['jobs']:
        job['usage'] = usage['jobs'].get(job.get('attempt_id')) if job['status'] in ACTIVE else None
    for observation in guard['observations']:
        observation['usage'] = usage['external'].get(observation['id'])
    if state.get('scheduler', {}).get('mode') == 'bounded-sharing':
        from .sharing import EvidenceWindow, evaluate
        if not hasattr(sampler, '_sharing_window'):
            sampler._sharing_window = EvidenceWindow()
        state['sharing_evidence'] = sampler._sharing_window.add(usage, source='client')
        state['admission'] = evaluate(state, guard, state['sharing_evidence'])
        for job in state['jobs']:
            if job['status'] in PENDING:
                job['reason'] = evaluate(state, guard, state['sharing_evidence'], candidate_id=job['id'])['reason']
    return state
