"""Endpoint-owned telemetry cadence, independent of frontend status polling."""
from __future__ import annotations

from copy import deepcopy
import math
import threading
import time

from cmw.jobs.activity import Observer
from cmw.jobs.freshness import age_seconds, clock_stamp
from cmw.jobs.sharing import EvidenceWindow, fingerprint
from cmw.jobs.store import ACTIVE, JobsError
from cmw.jobs import telemetry as jobs_telemetry
from cmw.jobs.telemetry import MAX_AGE, empty_usage
from cmw.jobs.thermal import age_thermal

INTERVAL = 2.0
UNAVAILABLE_AFTER = 15.0


def _job_binding(job):
    return {key: deepcopy(job.get(key)) for key in
            ('id', 'attempt_id', 'worker', 'group', 'engine', 'resources', 'env')}


def _external_binding(observation):
    try:
        return fingerprint(observation)
    except (KeyError, TypeError, ValueError, JobsError):
        return None


class _SampleWindow:
    """Polling reads the cadence window; it cannot count one sample twice."""
    def __init__(self, provider):
        self.provider = provider

    def add(self, usage, *, source):
        return self.provider.evidence(source=source)


class NodeTelemetry:
    """One bounded sampler worker per endpoint; reads never wait for collection.

    Two-second starts leave margin below Jobs' five-second CPU-baseline cutoff.
    Slow cycles skip missed slots instead of creating overlapping/catch-up work.
    Samples are stale after five seconds and unavailable after fifteen seconds;
    a stopped/failed sampler is stale immediately. Node monotonic age prevents
    wall-clock adjustment from reviving retained evidence. Restart requires the
    old worker to finish and creates a new sampler, hence new CPU baselines.
    """
    def __init__(self, store, *, sampler_factory=None, observer_factory=Observer,
                 interval=INTERVAL, monotonic=time.monotonic, wall_time=time.time,
                 autostart=True):
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 < interval < MAX_AGE:
            raise ValueError('Node telemetry interval must be positive and below five seconds')
        self.store = store
        self.sampler_factory = sampler_factory or (lambda: jobs_telemetry.Sampler(thermal=None))
        self.observer_factory = observer_factory
        self.interval, self.monotonic, self.wall_time = interval, monotonic, wall_time
        self._lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._sampler = None
        self._retired_sampler = None
        self._observer = None
        self._window = EvidenceWindow()
        self._sharing_window = _SampleWindow(self)
        self._running = False
        self._closed = False
        self._generation = 0
        self._last = None
        self._accepted_at = None
        self._job_bindings = {}
        self._external_bindings = {}
        self._error = 'Node telemetry has not sampled yet'
        if autostart:
            self.start()

    def start(self, *, background=True):
        """Start/restart without ever overlapping an unfinished previous worker."""
        with self._lock:
            previous = self._sampler or self._retired_sampler
            read_thread = getattr(previous, 'thread', None)
            observer_thread = getattr(self._observer, 'thread', None)
            if (self._closed or self._running or self._collect_lock.locked()
                    or (self._thread is not None and self._thread.is_alive())
                    or (isinstance(read_thread, threading.Thread) and read_thread.is_alive())
                    or (isinstance(observer_thread, threading.Thread) and observer_thread.is_alive())):
                return False
            if self._sampler is not None:
                self._sampler.close()
            self._sampler = self.sampler_factory()
            self._retired_sampler = None
            self._observer = self.observer_factory()
            self._window = EvidenceWindow()
            self._generation += 1
            self._stop = threading.Event()
            self._running = True
            self._error = 'Node telemetry is warming up after start'
            self._last = None
            self._accepted_at = None
            self._job_bindings, self._external_bindings = {}, {}
            if background:
                self._thread = threading.Thread(target=self._run, args=(self._generation, self._stop),
                                                daemon=True, name='cmw-node-telemetry')
                self._thread.start()
            else:
                self._thread = None
            return True

    def _run(self, generation, stop):
        try:
            while not stop.is_set():
                started = self.monotonic()
                self.collect_once(generation=generation)
                # A slow sample gets a full quiet interval, never a catch-up burst.
                elapsed = max(0.0, self.monotonic() - started)
                delay = self.interval - elapsed if elapsed < self.interval else self.interval
                if stop.wait(delay):
                    break
        finally:
            with self._lock:
                if generation == self._generation:
                    self._running = False
                    self._error = 'Node telemetry sampler stopped'

    def collect_once(self, *, generation=None):
        """Collect one cadence tick; injectable observers make this testable offline."""
        if not self._collect_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                generation = self._generation if generation is None else generation
                if not self._running or generation != self._generation:
                    return False
                sampler, observer = self._sampler, self._observer
            started = self.monotonic()
            try:
                state = self.store.snapshot()
                state['_telemetry_membership'] = True
                guard = observer.scan(self.store, state, source='client')
                state.pop('_telemetry_membership', None)
                pids = guard.pop('_pids', None)
                if guard.get('stale'):
                    pids = None
                result = sampler.sample(self.store, state, guard, pids)
                completed = self.monotonic()
                with self._lock:
                    if not self._running or generation != self._generation:
                        return False
                    result = deepcopy(result)
                    bindings = {
                        'jobs': {j['attempt_id']: _job_binding(j) for j in state['jobs'] if j['status'] in ACTIVE},
                        'external': {o['id']: _external_binding(o) for o in guard['observations']},
                    }
                    previous_bindings = {'jobs': self._job_bindings, 'external': self._external_bindings}
                    for section, key, value in self._values(result):
                        prior = (self._last or {}).get(section, {})
                        prior = prior if section == 'machine_usage' else prior.get(key, {})
                        previous_age = 0.0
                        stamp = value.get('observed_clock') or value.get('observed_at')
                        prior_stamp = prior.get('observed_clock') or prior.get('observed_at')
                        retained = stamp is not None and stamp == prior_stamp
                        if self._accepted_at is not None and retained:
                            previous_age = prior.get('age_seconds', 0.0) + max(0.0, completed - self._accepted_at)
                        if section != 'machine_usage' and (retained or value.get('stale')):
                            # A fallback is evidence about its original owner,
                            # never the current inputs of the failed collection.
                            bindings[section][key] = previous_bindings[section].get(key) if retained else None
                        stamp = value.get('observed_at')
                        age = max(0.0, float(value.get('age_seconds') or 0.0), previous_age,
                                  completed-started)
                        value['age_seconds'] = age if stamp is not None else None
                    self._last, self._accepted_at = result, completed
                    self._job_bindings, self._external_bindings = bindings['jobs'], bindings['external']
                    self._error = ''
                    self._window.add(deepcopy(result), source='client', monotonic=completed)
                    return True
            except Exception as exc:
                with self._lock:
                    if self._running and generation == self._generation:
                        self._error = f'Node telemetry sample failed: {type(exc).__name__}'
                return False
        finally:
            self._collect_lock.release()

    @staticmethod
    def _values(result):
        yield 'machine_usage', None, result['machine_usage']
        for section in ('jobs', 'external'):
            for key, value in result[section].items():
                yield section, key, value

    def _age(self, value, *, current, elapsed, stopped=False, error=''):
        result = deepcopy(value)
        stamp = result.get('observed_at')
        if stamp is None:
            result.update(stale=True, reason=error or result.get('reason') or 'Node telemetry has not sampled yet',
                          sampler_status='unavailable')
            return result
        sample_age = age_seconds(result.get('observed_clock'), current)
        age = max(float(result.get('age_seconds') or 0.0) + elapsed, sample_age or 0.)
        result['age_seconds'] = age if sample_age is not None else None
        unavailable = age > UNAVAILABLE_AFTER
        stale = bool(result.get('stale') or sample_age is None or stopped or error or age > MAX_AGE)
        result.update(stale=stale, sampler_status='unavailable' if unavailable else 'stale' if stale else 'running')
        if unavailable:
            for field in ('cpu_percent', 'cpu_cores', 'rss_bytes', 'footprint_bytes', 'ram_used_bytes',
                          'ram_total_bytes', 'ram_available_bytes', 'swap_used_bytes', 'swap_total_bytes', 'swap_out_bytes'):
                if field in result:
                    result[field] = None
            if 'memory_categories_bytes' in result:
                result['memory_categories_bytes'] = {}
            if 'process_samples' in result:
                result['process_samples'] = []
            for field in ('cpu_quality', 'memory_quality', 'footprint_quality', 'coverage_quality'):
                if field in result:
                    result[field] = 'unavailable'
        elif stale:
            for field in ('cpu_quality', 'memory_quality', 'footprint_quality'):
                if result.get(field) in ('fresh', 'partial', 'warming-up'):
                    result[field] = 'stale'
        if stale:
            result['reason'] = (error or ('Node telemetry sampler stopped' if stopped else
                                'Sample lacks comparable boot and time evidence' if sample_age is None else
                                'Node telemetry sample expired'))
        if 'thermal' in result:
            result['thermal'] = age_thermal(result['thermal'], now=self.wall_time()-elapsed, elapsed=elapsed,
                                            unavailable=stopped)
        return result

    def sample(self, store, state, guard, pids):
        """Jobs.project adapter: returns cached observations, performs no sampling."""
        with self._lock:
            last = self._last or {'machine_usage': jobs_telemetry.Sampler._empty_machine(), 'jobs': {}, 'external': {}}
            now = self.monotonic()
            elapsed = max(0.0, now - self._accepted_at) if self._accepted_at is not None else 0.0
            options = dict(current=clock_stamp(now), elapsed=elapsed, stopped=not self._running, error=self._error)
            result = {'machine_usage': self._age(last['machine_usage'], **options), 'jobs': {}, 'external': {}}
            for job in state['jobs']:
                if job['status'] not in ACTIVE:
                    continue
                key = job['attempt_id']
                usage = last['jobs'].get(key)
                if usage is None or self._job_bindings.get(key) != _job_binding(job):
                    usage = empty_usage('Node telemetry has no sample for this attempt/owner binding')
                result['jobs'][key] = self._age(usage, **options)
            for observation in guard['observations']:
                key = observation['id']
                binding = _external_binding(observation)
                usage = last['external'].get(key)
                if usage is None or binding is None or self._external_bindings.get(key) != binding:
                    usage = empty_usage('Node telemetry has no sample for this external membership')
                external_options = {**options, 'error': 'External membership observation stale'} if guard.get('stale') else options
                result['external'][key] = self._age(usage, **external_options)
            return result

    def evidence(self, *, source='client'):
        """Return actual cadence samples, aged without appending poll-time copies."""
        with self._lock:
            samples = deepcopy(self._window.samples)
            current = clock_stamp(self.monotonic())
            for sample in samples:
                elapsed = age_seconds(sample.get('observed_clock'), current)
                if elapsed is None:
                    return {'samples': [], 'source': source}
                for section, key, value in self._values(sample):
                    aged = self._age(value, current=current, elapsed=elapsed, stopped=not self._running, error=self._error)
                    if section == 'machine_usage':
                        sample[section] = aged
                    else:
                        sample[section][key] = aged
            return {'samples': samples, 'source': source}

    def stop(self):
        with self._lock:
            self._running = False
            self._generation += 1
            self._stop.set()
            thread, sampler = self._thread, self._sampler
            self._sampler = None
            if sampler is not None:
                self._retired_sampler = sampler
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.1)
        if sampler is not None:
            sampler.close()

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()
