"""Private runner IPC and sleep-inclusive clocks for controlled suspension.

Only the existing Jobs worker signals the computational domain. Participating
runners acknowledge durable boundaries and keep producers/finalization held.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import time
from uuid import uuid4

import psutil

from .ownership import group_members, identity, owner_alive

PROTOCOL = 1
BINDING_FIELDS = ('protocol', 'backend', 'job_id', 'attempt_id', 'claim', 'worker',
                  'anchor', 'state_identity', 'nonce', 'directory_identity', 'clock')
MAX_RECORD_BYTES = 262144


class SuspensionProtocolError(ValueError):
    """A private runner capability or acknowledgement cannot be trusted."""


def now_clock():
    """Seconds on a monotonic clock that includes system sleep, on this boot."""
    if sys.platform == 'darwin':
        library = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        class Timebase(ctypes.Structure):
            _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
        base = Timebase()
        library.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        if library.mach_timebase_info(ctypes.byref(base)) != 0 or not base.denom:
            raise SuspensionProtocolError('Continuous clock timebase unavailable')
        library.mach_continuous_time.restype = ctypes.c_uint64
        return library.mach_continuous_time() * base.numer / base.denom / 1_000_000_000
    if sys.platform.startswith('linux') and hasattr(time, 'CLOCK_BOOTTIME'):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    raise SuspensionProtocolError('A qualified sleep-inclusive clock is unavailable')


def clock_descriptor():
    now_clock()  # Refuse platforms whose advertised clock cannot be read.
    return {'kind': 'mach_continuous_time' if sys.platform == 'darwin' else 'CLOCK_BOOTTIME',
            'boot': psutil.boot_time()}


def _directory_identity(path):
    path = Path(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise SuspensionProtocolError(f'Required directory unavailable: {path}')
    return {'path': str(path.resolve(strict=True)), 'device': info.st_dev, 'inode': info.st_ino}


def _private_info(info, *, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SuspensionProtocolError('Suspension IPC must be private and owned by the current user')


def _read_fd(fd):
    info = os.fstat(fd)
    _private_info(info)
    if info.st_size > MAX_RECORD_BYTES:
        raise SuspensionProtocolError('Suspension IPC record is oversized')
    with os.fdopen(os.dup(fd), 'r', encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SuspensionProtocolError('Suspension IPC record must be an object')
    return value


class RunnerControl:
    """One exact-attempt runner; failed observation holds, never adopts or resumes."""

    @classmethod
    def from_environment(cls, backend, *, deadline, storage, runtime):
        value = os.environ.get('CMW_SUSPENSION_CONTEXT')
        if value is None:
            return None
        return cls(Path(value), backend=backend, deadline=deadline, storage=storage, runtime=runtime)

    def __init__(self, path, *, backend, deadline, storage, runtime):
        path = Path(path)
        if not path.is_absolute() or path.name != 'context.json' or path.is_symlink():
            raise SuspensionProtocolError('Invalid suspension context path')
        self.path = path
        self.directory = path.parent
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.lock_fd = None
        try:
            _private_info(os.fstat(self.fd), directory=True)
            self.context = self._read('context.json')
            if self.context.get('protocol') != PROTOCOL or self.context.get('backend') != backend:
                raise SuspensionProtocolError('Suspension backend/protocol mismatch')
            for key in BINDING_FIELDS:
                if key not in self.context or self.context[key] is None:
                    raise SuspensionProtocolError(f'Missing suspension binding: {key}')
            if not all(isinstance(self.context.get(key), str) and self.context[key]
                       for key in ('attempt_id', 'claim', 'nonce')):
                raise SuspensionProtocolError('Invalid attempt/authority binding')
            job_id = self.context['job_id']
            if isinstance(job_id, bool) or not isinstance(job_id, (str, int)) or not job_id or (isinstance(job_id, int) and job_id < 1):
                raise SuspensionProtocolError('Invalid immutable job identity')
            self.binding = {key: self.context[key] for key in BINDING_FIELDS}
            self.clock = clock_descriptor()
            if self.context.get('clock') != self.clock:
                raise SuspensionProtocolError('Suspension clock/boot mismatch')
            self.runner = identity()
            self.deadline = dict(deadline)
            self.storage = [_directory_identity(value) for value in dict.fromkeys(storage)]
            self.runtime = dict(runtime)
            self.intervals = []
            self.clock_started = None
            self._last_clock = now_clock()
            self._last_command = None
            self._last_ack_clock = None
            self._last_ack_status = None
            self._state = 'run'
            self.last_error = ''
            self.accounting_uncertain = False
            self._registered = False
            self._check_authority()
            self.lock_fd = os.open('runner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                   0o600, dir_fd=self.fd)
            _private_info(os.fstat(self.lock_fd))
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SuspensionProtocolError('A runner already owns this attempt') from exc
            if self._read('registration.json', optional=True) is not None:
                raise SuspensionProtocolError('This attempt already has a runner registration')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _read(self, name, *, optional=False):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        except FileNotFoundError:
            if optional:
                return None
            raise
        try:
            return _read_fd(fd)
        finally:
            os.close(fd)

    def _write(self, name, value):
        self._check_directory()
        temporary = f'.{name}.{uuid4().hex}'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(value, handle, sort_keys=True, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass
            raise

    def _check_directory(self):
        current = self.directory.lstat()
        pinned = os.fstat(self.fd)
        _private_info(current, directory=True)
        expected = self.context.get('directory_identity', {})
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino) or (
                current.st_dev, current.st_ino) != (expected.get('device'), expected.get('inode')):
            raise SuspensionProtocolError('Suspension control directory was replaced')

    def _check_authority(self, *, check_storage=True):
        self._check_directory()
        if self._read('context.json') != self.context:
            raise SuspensionProtocolError('Suspension context changed')
        if clock_descriptor() != self.clock:
            raise SuspensionProtocolError('Suspension clock/boot changed')
        if not owner_alive(self.context.get('worker')) or not owner_alive(self.context.get('anchor')):
            raise SuspensionProtocolError('Suspension worker or ownership anchor is unavailable')
        anchor = self.context['anchor']['pid']
        if os.getsid(os.getpid()) != anchor or os.getsid(anchor) != anchor or os.getpgid(anchor) != anchor:
            raise SuspensionProtocolError('Runner is outside its bound owned session')
        current = now_clock()
        if current < self._last_clock:
            raise SuspensionProtocolError('Suspension clock moved backwards')
        self._last_clock = current
        if check_storage:
            self._check_storage()

    def _check_storage(self):
        for item in self.storage:
            if _directory_identity(item['path']) != item:
                raise SuspensionProtocolError(f"Required storage identity changed: {item['path']}")

    def require_storage(self, paths):
        """Add runner-created directories before the immutable registration."""
        if self._registered:
            raise SuspensionProtocolError('Storage registration is already sealed')
        self._check_authority()
        known = {item['path'] for item in self.storage}
        for path in paths:
            value = _directory_identity(path)
            if value['path'] not in known:
                self.storage.append(value)
                known.add(value['path'])

    def register(self, root_pid, strategy='direct-tree', protected_pids=()):
        if self._registered or strategy not in {'direct-tree', 'openmpi', 'orca-tree'}:
            raise SuspensionProtocolError('Duplicate or unsupported domain registration')
        self._check_authority()
        root = identity(root_pid)
        if os.getsid(root_pid) != self.context['anchor']['pid'] or root_pid == self.runner['pid']:
            raise SuspensionProtocolError('Computational root is outside the owned session')
        protected = [identity(pid) for pid in protected_pids]
        if any(item['pid'] == root_pid for item in protected):
            raise SuspensionProtocolError('Computational root cannot be protected')
        self.clock_started = now_clock()
        self._write('registration.json', {**self.binding, 'backend': self.context['backend'],
                    'runner': self.runner, 'root': root, 'strategy': strategy,
                    'protected': protected, 'deadline': self.deadline, 'storage': self.storage,
                    'runtime': self.runtime, 'clock': self.clock, 'clock_started': self.clock_started})
        self._registered = True

    def domain_drained(self):
        """Read-only finalization gate after the computational root really exits."""
        try:
            self._check_directory()
            if self._read('context.json') != self.context:
                raise SuspensionProtocolError('Suspension context changed')
            anchor = self.context['anchor']
            if not owner_alive(anchor) or os.getsid(os.getpid()) != anchor['pid']:
                raise SuspensionProtocolError('Owned session cannot be verified for drainage')
            protected = {os.getpid(), anchor['pid']}
            for parent in psutil.Process().parents():
                if os.getsid(parent.pid) != anchor['pid']:
                    break
                protected.add(parent.pid)
                if parent.pid == anchor['pid']:
                    break
            remaining = group_members(anchor['pid'], session=True)
            # MPI modules may outlive their serial driver or group leader. Only
            # the existing anchor-to-runner control chain may remain at return.
            return not set(remaining).difference(protected) and owner_alive(anchor)
        except (OSError, ValueError, psutil.Error) as exc:
            self.last_error = str(exc)
            return False

    @property
    def elapsed(self):
        return max(0.0, now_clock() - self.clock_started) if self.clock_started is not None else 0.0

    @property
    def suspended_seconds(self):
        now = now_clock()
        return sum(max(0.0, (item['end'] if item.get('end') is not None else now) - item['start'])
                   for item in self.intervals)

    @property
    def unsuspended_elapsed(self):
        return max(0.0, self.elapsed - self.suspended_seconds)

    def execution_budget_exhausted(self, seconds):
        return seconds is not None and self.unsuspended_elapsed >= seconds

    def _ack(self, command, status):
        self._last_ack_status = status
        self._last_ack_clock = now_clock()
        self._write('ack.json', {**self.binding, 'revision': command['revision'],
                    'request_id': command['request_id'], 'operation': command['operation'],
                    'runner': self.runner, 'status': status, 'intervals': self.intervals,
                    'suspended_seconds': self.suspended_seconds, 'clock': self.clock,
                    'deadline': self.deadline, 'observed_clock': now_clock(), 'clock_started': self.clock_started,
                    'unsuspended_elapsed': self.unsuspended_elapsed,
                    'accounting_uncertain': self.accounting_uncertain})

    def _apply(self, command):
        if any(command.get(key) != value for key, value in self.binding.items()):
            raise SuspensionProtocolError('Suspension command does not match this authority/attempt')
        revision = command.get('revision')
        request = command.get('request_id')
        operation = command.get('operation')
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 or not isinstance(request, str) or not request:
            raise SuspensionProtocolError('Invalid suspension request identity')
        if operation not in {'prepare_suspend', 'suspended', 'prepare_continue', 'run', 'cancel', 'invalidate'}:
            raise SuspensionProtocolError('Invalid suspension operation')
        previous = self._last_command
        if previous:
            if revision < previous['revision'] or (revision == previous['revision'] and request != previous['request_id']):
                raise SuspensionProtocolError('Stale suspension request')
            if command == previous:
                if self._last_ack_clock is not None and now_clock() - self._last_ack_clock >= 1:
                    self._ack(command, self._last_ack_status)
                return
            if revision == previous['revision'] and operation != 'invalidate' and (previous['operation'], operation) not in {
                    ('prepare_suspend', 'suspended'), ('prepare_continue', 'run'),
                    ('prepare_suspend', 'cancel'), ('suspended', 'cancel'), ('prepare_continue', 'cancel'), ('run', 'cancel'), ('invalidate', 'cancel')}:
                raise SuspensionProtocolError('Contradictory suspension operation replay')
        status = {'prepare_suspend': 'prepared', 'suspended': 'paused',
                  'prepare_continue': 'resume_prepared', 'run': 'running', 'cancel': 'cancel_prepared',
                  'invalidate': 'invalidated'}[operation]
        if operation == 'invalidate':
            self._mark_uncertain(self._boundary(command))
            self._state = 'hold'
        elif operation == 'prepare_suspend':
            if self._state != 'run':
                raise SuspensionProtocolError('Suspension preparation requires a running runner')
            self._state = 'hold'
        elif operation == 'suspended':
            if not previous or previous['operation'] != 'prepare_suspend' or self._state != 'hold':
                raise SuspensionProtocolError('Suspension acknowledgement lacks preparation')
            boundary = self._boundary(command)
            self.intervals.append({'start': boundary, 'end': None,
                                   'request_id': request, 'revision': revision})
        elif operation == 'prepare_continue':
            if self._state != 'hold' or not self.intervals or self.intervals[-1].get('end') is not None:
                raise SuspensionProtocolError('Continuation requires a confirmed suspension')
            boundary = self._boundary(command)
            if boundary < self.intervals[-1]['start']:
                raise SuspensionProtocolError('Continuation boundary precedes suspension')
            self.intervals[-1]['end'] = boundary
        elif operation == 'run':
            if not previous or previous['operation'] != 'prepare_continue':
                raise SuspensionProtocolError('Continuation release lacks preparation')
            self._state = 'run'
        else:
            if self.intervals and self.intervals[-1].get('end') is None:
                self.intervals[-1]['end'] = now_clock()
            self._state = 'cancel'
        self._last_command = dict(command)
        self._ack(command, status)

    def _boundary(self, command):
        value = command.get('boundary')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SuspensionProtocolError('Invalid suspension clock boundary')
        if self.clock_started is None or not self.clock_started <= value <= now_clock():
            raise SuspensionProtocolError('Suspension boundary outside this execution')
        return float(value)

    def _mark_uncertain(self, boundary=None):
        self.accounting_uncertain = True
        if self.intervals and self.intervals[-1].get('end') is None:
            start = self.intervals[-1]['start']
            selected = boundary if boundary is not None else self._last_ack_clock
            if selected is None or not math.isfinite(selected):
                selected = start
            self.intervals[-1]['end'] = max(start, selected)

    def poll(self):
        try:
            self._check_authority(check_storage=False)
            command = self._read('command.json', optional=True)
            # Missing storage forbids execution release, not authenticated
            # termination preparation for already-owned processes.
            if command is None or command.get('operation') not in {'cancel', 'invalidate'}:
                self._check_storage()
            if command is not None:
                self._apply(command)
            self.last_error = ''
            return self._state
        except (OSError, ValueError, psutil.Error) as exc:
            self._mark_uncertain()
            self.last_error = str(exc)
            return 'hold'

    def request_termination(self, reason, exit_code=124):
        self._check_authority(check_storage=False)
        self._state = 'cancel'
        self._write('deadline_expired.json', {**self.binding, 'runner': self.runner,
                    'reason': str(reason), 'exit_code': int(exit_code), 'clock': self.clock,
                    'recorded_clock': now_clock(), 'intervals': self.intervals})


def _positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Expected a finite positive duration')
    return number


def file_identity(path):
    path = Path(path).resolve(strict=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise SuspensionProtocolError(f'Runtime executable is unavailable: {path}')
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino}


def _mpi_configuration_files(launcher_path, control_runtime):
    """Reject effective user/system overrides whose watchdog/topology is unknown."""
    inspectors = [launcher_path.parent / 'ompi_info']
    if control_runtime:
        inspectors.append(Path(control_runtime['path']).parent / 'prte-info')
    directories = set()
    for inspector in inspectors:
        file_identity(inspector)
        output = subprocess.run([str(inspector), '--parsable', '--path', 'sysconfdir'],
                                check=True, capture_output=True, text=True, timeout=5).stdout
        values = [line.removeprefix('path:sysconfdir:') for line in output.splitlines()
                  if line.startswith('path:sysconfdir:')]
        if len(values) != 1 or not Path(values[0]).is_absolute():
            raise SuspensionProtocolError('MPI configuration directory is not established')
        directories.add(Path(values[0]))
    families = ('openmpi', 'pmix', 'prte') if control_runtime else ('openmpi', 'pmix')
    paths = [Path.home() / ('.' + family) / 'mca-params.conf' for family in families]
    paths.extend(directory / (family + '-mca-params.conf')
                 for directory in sorted(directories) for family in families)
    paths.extend(directory / 'openmpi-mca-params-override.conf' for directory in sorted(directories))
    for path in paths:
        if not path.exists():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise SuspensionProtocolError(f'Unsupported MPI configuration file: {path}')
        if any(line.strip() and not line.lstrip().startswith('#') for line in path.read_text().splitlines()):
            raise SuspensionProtocolError(f'Unqualified MPI configuration directives: {path}')
    return [str(path) for path in paths]


def mpi_runtime_identity(launcher, ranks):
    """Describe explicitly selected, locally qualified MPI families without launch."""
    if isinstance(ranks, bool) or not isinstance(ranks, int) or ranks < 1:
        raise SuspensionProtocolError('MPI ranks must be a positive integer')
    scheduler = [key for key in os.environ if key.startswith(('SLURM_', 'PBS_', 'SGE_', 'LSB_'))]
    if scheduler:
        raise SuspensionProtocolError('Scheduler allocation environment is outside the local-only contract: ' + ', '.join(sorted(scheduler)))
    selected = file_identity(launcher)
    path = Path(selected['path'])
    observed_version = subprocess.run([str(launcher), '--version'], check=True, capture_output=True,
                                      text=True, timeout=5).stdout.strip().splitlines()[0]
    version = ('mpirun (Open MPI) 4.1.6' if observed_version == 'orterun (OpenRTE) 4.1.6'
               else observed_version)
    if version not in {'mpirun (Open MPI) 4.1.6', 'mpirun (Open MPI) 5.0.9'}:
        raise SuspensionProtocolError('MPI version has no qualified suspension backend')
    forbidden = [key for key in os.environ if key.startswith(('OMPI_MCA_', 'PRTE_MCA_', 'PMIX_MCA_'))
                 and key not in {'PMIX_MCA_gds', 'PMIX_MCA_plog', 'OMPI_MCA_osc_sm_backing_directory'}]
    if forbidden:
        raise SuspensionProtocolError('Unknown MPI configuration overrides: ' + ', '.join(sorted(forbidden)))
    controls = []
    launcher_executables = [selected]
    control_runtime = None
    if '4.1.6' in version:
        controls.append(file_identity(path.parent / 'orted'))
    else:
        # Homebrew installs PRRTE as a separately versioned dependency. Record the
        # actual executable identity; never qualify it merely by process name.
        control = shutil.which('prted', path=str(path.parent) + os.pathsep + os.environ.get('PATH', ''))
        if control:
            controls.append(file_identity(control))
        launcher_alias = shutil.which('prterun', path=str(path.parent) + os.pathsep + os.environ.get('PATH', ''))
        if not launcher_alias:
            raise SuspensionProtocolError('Qualified MPI5 PRRTE launcher is unavailable')
        alias = file_identity(launcher_alias)
        alias_version = subprocess.run([launcher_alias, '--version'], check=True, capture_output=True,
                                       text=True, timeout=5).stdout.strip().splitlines()[0]
        if alias_version not in {'prterun (PRRTE) 4.1.0', 'prte (PRRTE) 4.1.0'}:
            raise SuspensionProtocolError('PRRTE version has no qualified suspension backend')
        launcher_executables.append(alias)
        control_runtime = {**alias, 'version': alias_version}
    configuration_files = _mpi_configuration_files(path, control_runtime)
    return {'mpi_launcher': {**selected, 'version': version, 'observed_version': observed_version}, 'ranks': ranks,
            'configuration_files_checked': configuration_files,
            'control_executables': controls, 'local_only': True,
            'launcher_executables': launcher_executables, 'control_runtime': control_runtime,
            'mpi_implementation': 'Open MPI',
            'native_engine_qualified': False, 'sleep_qualified': False}


def controlled_driver(command, *, backend, execution_budget=None, hard_timeout=None,
                      mpi_launcher=None, mpi_ranks=2, storage=(), runtime=None, strategy=None):
    """Launch one foreground domain; only its bound worker can stop or cancel it."""
    deadline = {'protocol': PROTOCOL, 'clock': clock_descriptor(),
                'execution_budget_seconds': execution_budget, 'hard_timeout_seconds': hard_timeout,
                'external_watchdogs': 'none-declared', 'owner': 'runner', 'known': True}
    details = {'local_only': True, **dict(runtime or {})}
    workload_executable = shutil.which(command[0]) or command[0]
    runtime_storage = [str(Path(workload_executable).resolve(strict=True).parent)]
    child_environment = dict(os.environ)
    if mpi_launcher:
        details.update(mpi_runtime_identity(mpi_launcher, mpi_ranks))
        runtime_storage.extend(str(Path(item['path']).parent) for item in
                               [*details['launcher_executables'], *details['control_executables']])
        launcher = details['mpi_launcher']['path']
        forwarding = (['--mca', 'ess_base_forward_signals', 'SIGTSTP,SIGCONT']
                      if '4.1.6' in details['mpi_launcher']['version'] else
                      ['--forward-signals', 'SIGTSTP,SIGCONT'])
        topology = []
        if sys.platform == 'darwin' and '5.0.9' in details['mpi_launcher']['version']:
            cores = int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.physicalcpu'], text=True))
            child_environment['HWLOC_SYNTHETIC'] = f'Package:1 Core:{cores} PU:1'
            details['topology'] = {'mode': 'synthetic-local', 'hwloc': child_environment['HWLOC_SYNTHETIC']}
            topology = ['--bind-to', 'none', '--map-by', 'slot']
        command = [launcher, '--host', f'localhost:{mpi_ranks}', *forwarding, *topology, '-n', str(mpi_ranks), *command]
    control = RunnerControl.from_environment(backend, deadline=deadline,
                                            storage=[str(Path.cwd()), *runtime_storage, *storage], runtime=details)
    if control is None:
        raise SuspensionProtocolError('Controlled execution requires an enrolled Jobs attempt')
    child = None
    prior = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            prior[signum] = signal.signal(signum, lambda *_: None)
        # This coordinator is single-threaded; setpgrp preserves the owned SID on
        # Python 3.10, without preexec work beyond the async-signal-safe syscall.
        child = subprocess.Popen(command, preexec_fn=os.setpgrp, env=child_environment)
        control.register(child.pid, strategy=strategy or ('openmpi' if mpi_launcher else 'direct-tree'))
        terminating = False
        while True:
            state = control.poll()
            result = child.poll()  # Popen waits for termination, never WUNTRACED stops.
            if result is not None and control.domain_drained():
                return result if result >= 0 else 128 - result
            if not terminating and ((hard_timeout is not None and control.elapsed >= hard_timeout) or
                                    control.execution_budget_exhausted(execution_budget)):
                try:
                    control.request_termination('hard wall deadline expired' if hard_timeout is not None and
                                                control.elapsed >= hard_timeout else 'execution budget expired')
                    terminating = True
                except (OSError, ValueError, psutil.Error) as exc:
                    # Lost authority cannot turn a deadline into wrapper exit or
                    # cleanup while the surviving computation is still alive.
                    control.last_error = str(exc)
            time.sleep(0.05)
    finally:
        for signum, handler in prior.items():
            signal.signal(signum, handler)
        # No ownership adoption or broad cleanup here; the worker retains its
        # pinned session until all registered computational members are drained.
        control.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', default='local-command-v1')
    parser.add_argument('--execution-budget', type=_positive)
    parser.add_argument('--hard-timeout', type=_positive)
    parser.add_argument('--mpi-launcher')
    parser.add_argument('--mpi-ranks', type=int, default=2)
    parser.add_argument('--storage', action='append', default=[])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('A foreground command is required')
    try:
        return controlled_driver(command, backend=args.backend, execution_budget=args.execution_budget,
                                 hard_timeout=args.hard_timeout, mpi_launcher=args.mpi_launcher,
                                 mpi_ranks=args.mpi_ranks, storage=args.storage)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Controlled runner refused: {exc}', file=sys.stderr)
        return 69


if __name__ == '__main__':
    raise SystemExit(main())
