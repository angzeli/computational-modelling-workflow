"""Optional, ephemeral CPU sensor observations; never an admission input.

Only an explicitly configured macmon 0.10.x executable is supported. Its finite
JSON pipe invocation runs in one worker, independently of CPU/RAM sampling. No
provider output is executed or copied verbatim into diagnostics.
"""
from __future__ import annotations

import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import stat
import subprocess
import threading
import time

INTERVAL = 20.0
FRESH_SECONDS = 30.0
UNAVAILABLE_SECONDS = 60.0
TEMPERATURE_KIND = 'average CPU sensors'
UNITS = 'Celsius'
MAX_OUTPUT = 65536
MAX_STDERR = 4096
MAX_FAILURES = 3


def _number(value):
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except (OverflowError, TypeError):
        return False


def valid_temperature(value):
    """Zero is an unavailable sensor sentinel; this is not an alarm threshold."""
    return _number(value) and 0 < value <= 150


def unavailable_thermal(reason='Optional CPU temperature provider not configured'):
    return dict(temperature_celsius=None, temperature_kind=TEMPERATURE_KIND,
                units=UNITS, source='macmon', sample_timestamp=None,
                sample_age=None, quality='unavailable', availability_reason=reason,
                freshness_seconds=FRESH_SECONDS, unavailable_seconds=UNAVAILABLE_SECONDS)


def age_thermal(value, now=None, *, elapsed=0.0, unavailable=False):
    """Age host telemetry independently; now=0 uses only its recorded age.

Remote callers add elapsed receipt/request time without comparing host clocks.
The original sample timestamp is retained, including on frozen Copy snapshots.
"""
    if not isinstance(value, dict):
        return unavailable_thermal()
    result = copy.deepcopy(value)
    valid = (value.get('source') == 'macmon' and value.get('temperature_kind') == TEMPERATURE_KIND
             and value.get('units') == UNITS and valid_temperature(value.get('temperature_celsius'))
             and value.get('quality') in ('fresh', 'stale')
             and _number(value.get('sample_timestamp')) and value['sample_timestamp'] > 0
             and _number(value.get('sample_age')) and value['sample_age'] >= 0)
    if not valid:
        result.update(temperature_celsius=None, quality='unavailable')
        if not _number(result.get('sample_age')) or result['sample_age'] < 0:
            result['sample_age'] = None
        if not _number(result.get('sample_timestamp')) or result['sample_timestamp'] <= 0:
            result['sample_timestamp'] = None
        result['availability_reason'] = ('Invalid or unsupported CPU temperature observation'
                                        if value.get('quality') != 'unavailable' else
                                        value.get('availability_reason', 'CPU temperature unavailable'))
        reason = result['availability_reason']
        result['availability_reason'] = reason[:256] if isinstance(reason, str) else 'CPU temperature unavailable'
        return result
    if not _number(elapsed) or elapsed < 0:
        return unavailable_thermal('Invalid CPU temperature observation age')
    age = max(value['sample_age'],
              max(0.0, (time.time() if now is None else now)-value['sample_timestamp'])) + elapsed
    result['sample_age'] = age
    if age > UNAVAILABLE_SECONDS:
        result.update(temperature_celsius=None, quality='unavailable',
                      availability_reason='CPU temperature sample expired')
    elif unavailable or age > FRESH_SECONDS or value['quality'] == 'stale':
        result.update(quality='stale')
        if unavailable:
            result['availability_reason'] = 'Last reported CPU temperature; node unavailable'
        elif not result.get('availability_reason'):
            result['availability_reason'] = 'CPU temperature sample is stale'
    return result


class ThermalError(ValueError):
    """A bounded, non-sensitive diagnostic, never raw provider output."""


def parse_macmon(output, *, started_at, received_at):
    """Read the complete single-sample JSON document, not individual pipe reads."""
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        doc = json.loads(output, object_pairs_hook=unique_pairs,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ThermalError('Malformed or multiple macmon JSON frames') from None
    if (not isinstance(doc, dict) or not isinstance(doc.get('temp'), dict)
            or 'schema_version' in doc):
        raise ThermalError('Unsupported macmon temperature schema')
    temp = doc['temp']
    if 'cpu_temp_avg' not in temp:
        raise ThermalError('macmon CPU average sensor field missing')
    if (temp.get('units', UNITS) not in (UNITS, 'C') or
            doc.get('temperature_units', UNITS) not in (UNITS, 'C')):
        raise ThermalError('Unsupported macmon temperature units')
    value = temp['cpu_temp_avg']
    if value is None:
        raise ThermalError('macmon CPU average sensors unavailable')
    if not valid_temperature(value):
        raise ThermalError('Invalid macmon CPU average sensor value')
    moment = started_at
    if 'timestamp' in doc:
        try:
            raw = doc['timestamp']
            if not isinstance(raw, str):
                raise ValueError
            stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError
            moment = stamp.timestamp()
        except (ValueError, TypeError, OverflowError):
            raise ThermalError('Invalid macmon sample timestamp') from None
        if moment > received_at + 1.0:
            raise ThermalError('macmon sample timestamp is in the future')
    result = unavailable_thermal('')
    result.update(temperature_celsius=float(value), sample_timestamp=moment,
                  sample_age=max(0.0, received_at-moment), quality='fresh')
    return age_thermal(result, now=received_at)


class ThermalProvider:
    """One worker and at most one owned child per observer, only when opted in.

Startup/help/sample operations are bounded. Three consecutive failures disable
this session; intermediate retries wait interval, then twice interval (<=120s).
A successful sample resets backoff. Shutdown never signals unrelated processes.
"""
    def __init__(self, executable=None, *, interval=INTERVAL, system=None, machine=None,
                 startup_timeout=2.0, sample_timeout=3.0, stop_timeout=0.5,
                 monotonic=time.monotonic, wall_time=time.time):
        self.monotonic, self.wall_time = monotonic, wall_time
        self.interval = interval
        self.startup_timeout, self.sample_timeout, self.stop_timeout = startup_timeout, sample_timeout, stop_timeout
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.process = None
        self.version = None
        self.failures = 0
        self.sample_count = 0
        self.launch_count = 0
        self.last = unavailable_thermal()
        self.sample_monotonic = None
        self.executable = None
        self.file_identity = None
        if executable is None:
            return
        if (system or platform.system()) != 'Darwin' or (machine or platform.machine()) not in {'arm64', 'aarch64'}:
            self.last = unavailable_thermal('CPU temperature provider supports Apple Silicon macOS only')
            return
        if not _number(interval) or not 15 <= interval <= 30:
            self.last = unavailable_thermal('Thermal interval must be 15–30 seconds')
            return
        try:
            path = Path(executable)
            if not path.is_absolute():
                raise ValueError
            path = path.resolve(strict=True)
            info = path.stat()
            if (not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK)
                    or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022):
                raise ValueError
            self.executable = str(path)
            self.file_identity = self._identity(info)
            self.last = unavailable_thermal('CPU temperature provider warming up')
        except (OSError, ValueError, TypeError):
            self.last = unavailable_thermal('Configured macmon executable unavailable or not trusted')

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    @classmethod
    def from_environment(cls):
        path = os.environ.get('CMW_THERMAL_MACMON') or None
        try:
            interval = float(os.environ.get('CMW_THERMAL_INTERVAL_SECONDS', str(INTERVAL)))
        except ValueError:
            interval = None
        return cls(path, interval=interval)

    def snapshot(self):
        with self.lock:
            if self.executable and self.thread is None and not self.stop.is_set():
                self.thread = threading.Thread(target=self._run, daemon=True, name='cmw-thermal')
                self.thread.start()
            result = copy.deepcopy(self.last)
            if self.sample_monotonic is not None:
                elapsed = max(0.0, self.monotonic()-self.sample_monotonic)
            else:
                elapsed = 0.0
        return age_thermal(result, now=0.0, elapsed=elapsed)

    def close(self):
        self.stop.set()
        thread = self.thread
        if thread is not None and thread.ident is not None and thread is not threading.current_thread():
            thread.join(2*self.stop_timeout + 0.5)

    def _command(self, args, timeout):
        if self.stop.is_set():
            raise ThermalError('CPU temperature observer closed')
        try:
            if self._identity(Path(self.executable).stat()) != self.file_identity:
                raise ThermalError('Configured macmon executable changed; reopen observer')
            process = subprocess.Popen([self.executable, *args], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       shell=False, close_fds=True)
        except OSError:
            raise ThermalError('Unable to start configured macmon executable') from None
        self.process = process
        self.launch_count += 1
        output, stderr_size = bytearray(), 0
        selector = selectors.DefaultSelector()
        deadline = self.monotonic()+timeout
        try:
            for name, stream in [('stdout', process.stdout), ('stderr', process.stderr)]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if self.stop.is_set():
                    raise ThermalError('CPU temperature observer closed')
                remaining = deadline-self.monotonic()
                if remaining <= 0:
                    raise ThermalError('macmon operation timed out')
                for key, _ in selector.select(min(0.1, remaining)):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == 'stdout':
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            raise ThermalError('macmon stdout exceeded bounded buffer')
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > MAX_STDERR:
                            raise ThermalError('macmon stderr exceeded bounded buffer')
            while process.poll() is None:
                if self.stop.wait(0.05):
                    raise ThermalError('CPU temperature observer closed')
                if self.monotonic() >= deadline:
                    raise ThermalError('macmon operation timed out')
            if process.returncode:
                raise ThermalError(f'macmon exited with status {process.returncode}; stderr bytes={stderr_size}')
            return bytes(output)
        finally:
            selector.close()
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=self.stop_timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=self.stop_timeout)
                else:
                    process.wait()
            finally:
                process.stdout.close()
                process.stderr.close()
                # Keep the owned handle on an exceptional unreaped child; the
                # worker must not start another process over it.
                if process.poll() is not None:
                    self.process = None

    def _run(self):
        checked = False
        while not self.stop.is_set():
            cycle_started = self.monotonic()
            try:
                if not checked:
                    output = self._command(['--version'], self.startup_timeout)
                    match = re.fullmatch(rb'macmon (0\.10\.\d+)\s*', output)
                    if not match:
                        raise ThermalError('Unsupported macmon version; expected 0.10.x')
                    self.version = match[1].decode('ascii')
                    help_text = self._command(['pipe', '--help'], self.startup_timeout)
                    if not all(word in help_text for word in [b'--samples', b'--interval', b'JSON']):
                        raise ThermalError('Unsupported macmon finite JSON pipe options')
                    checked = True
                started = self.wall_time()
                output = self._command(['pipe', '--samples', '1', '--interval', '1000'], self.sample_timeout)
                result = parse_macmon(output, started_at=started, received_at=self.wall_time())
                result['provider_version'] = self.version
                with self.lock:
                    self.last = result
                    self.sample_monotonic = self.monotonic()
                    self.failures = 0
                    self.sample_count += 1
                delay = max(0.0, self.interval-(self.monotonic()-cycle_started))
            except (ThermalError, OSError, ValueError, subprocess.SubprocessError) as exc:
                with self.lock:
                    self.failures += 1
                    self.last = unavailable_thermal(str(exc) if isinstance(exc, ThermalError) else
                                                    'CPU temperature provider failed')
                    self.sample_monotonic = None
                    self.last['provider_version'] = self.version
                    if self.failures >= MAX_FAILURES:
                        self.last['availability_reason'] += '; disabled for this observer after 3 failures'
                if self.failures >= MAX_FAILURES or self.process is not None:
                    break
                delay = min(120.0, self.interval * 2**(self.failures-1))
            if self.stop.wait(delay):
                break
