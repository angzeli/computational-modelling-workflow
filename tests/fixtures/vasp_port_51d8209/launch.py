#!/usr/bin/env python3
"""Single owner of MPI process-group lifetime; also used by run_vasp."""
import argparse
from datetime import datetime, timezone, timedelta
import math
import inspect
import os
import signal
import subprocess
import sys
import time

import toolchain

TOOLCHAIN, TOOLCHAIN_SOURCE = toolchain.load()
MPI_VERSION = TOOLCHAIN['PORT_OPENMPI_VERSION']
MPI = f'/opt/homebrew/Cellar/open-mpi/{MPI_VERSION}/bin/mpirun'
PRTED = f"/opt/homebrew/Cellar/prrte/{TOOLCHAIN['PORT_PRRTE_VERSION']}/bin/prted"



MAX_MPI_RANKS = 16
MAX_CONTROLLED_MPI_RANKS = 8


def mpi_command(program, ranks, mode, *, managed_foreground=False, controlled_suspension=False):
    if type(ranks) is not int or not 1 <= ranks <= MAX_MPI_RANKS or mode not in ('native', 'synthetic'):
        raise ValueError(f'ranks must be integers in 1..{MAX_MPI_RANKS}; mode must be native or synthetic')
    env = os.environ.copy()
    env.pop('HWLOC_SYNTHETIC', None)
    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    command = [MPI, '-np', str(ranks)]
    if controlled_suspension:
        if ranks > MAX_CONTROLLED_MPI_RANKS:
            raise ValueError(f'Controlled suspension supports at most {MAX_CONTROLLED_MPI_RANKS} ranks')
        if not managed_foreground:
            raise ValueError('Controlled suspension requires managed foreground ownership')
        inherited = sorted(key for key in env if key.startswith(('OMPI_MCA_', 'PRTE_MCA_', 'PMIX_MCA_',
                                                                 'SLURM_', 'PBS_', 'SGE_', 'LSB_')))
        if inherited:
            raise ValueError('Controlled suspension refuses inherited MPI/scheduler overrides: ' + ', '.join(inherited))
        command += ['--host', f'localhost:{ranks}', '--forward-signals', 'SIGTSTP,SIGCONT']
    if mode == 'synthetic':
        cores = int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.physicalcpu'], text=True))
        env['HWLOC_SYNTHETIC'] = f'Package:1 Core:{cores} PU:1'
        command += ['--bind-to', 'none', '--map-by', 'slot']
    # Preserve MPI's rank groups. Opt-in CMW ownership covers the dedicated
    # session, not just the outer launcher group.
    return command + list(program), env


def supervise(command, timeout, *, cwd=None, env=None, stdout=None, stderr=None,
              stop_before=0, on_stop=None, awake=False, grace=10, managed_foreground=False,
              control=None, execution_budget=None):
    """Own one session, forward INT/TERM, enforce a deadline and reap the leader.

    Internal command injection supports synthetic tests, not a public CLI option.
    The hard deadline excludes staging; termination may take up to grace seconds.
    """
    if not math.isfinite(timeout) or timeout <= 0 or not 0 <= stop_before < timeout:
        raise ValueError('timeout must be positive; 0 <= stop-before < timeout')
    if control is not None and (not managed_foreground or awake or stop_before):
        raise ValueError('Controlled suspension requires managed foreground, no advance stop, and no power assertion')
    if execution_budget is not None and (control is None or not math.isfinite(execution_budget) or execution_budget <= 0):
        raise ValueError('Pause-aware execution budget requires a coordinated control and a positive finite budget')
    managed_group = os.getpgrp() if managed_foreground else None
    if managed_foreground and (managed_group == os.getpid() or os.getsid(0) != managed_group
                               or os.environ.get("CMW_JOBS_OWN_SESSION") != "1"):
        raise ValueError('Managed foreground requires an inherited dedicated session/process group')
    if managed_foreground:
        from cmw.jobs.ownership import identity, owner_alive, group_members, signal_session
        if "include_leader" not in inspect.signature(signal_session).parameters:
            raise ValueError('Managed mode requires CMW protected-leader subgroup cleanup support')
        managed_owner = identity(managed_group)
        members = set(group_members(managed_group, session=True))
        if not owner_alive(managed_owner) or members != {managed_group, os.getpid()}:
            raise ValueError(f'Managed mode requires the sole foreground payload in a dedicated CMW session (leader={managed_group}, payload={os.getpid()}, members={sorted(members)})')
    requested = []
    previous = {}
    process = inhibitor = None
    reason, status, stop_note = 'completed', None, 'disabled' if not stop_before else 'not reached'
    started = time.monotonic()
    start_utc = datetime.now(timezone.utc).isoformat()

    def receive(signum, _frame):
        if not requested:
            requested.append(signum)

    def group_exists():
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def send_group(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def managed_members():
        if not owner_alive(managed_owner) or os.getsid(0) != managed_group or os.getpgrp() != managed_group:
            raise RuntimeError('Managed session authority changed; no local signal authorized')
        protected = {managed_group, os.getpid()}
        if inhibitor is not None:
            protected.add(inhibitor.pid)
        members = [pid for pid in group_members(managed_group, session=True) if pid not in protected]
        for pid in members:
            try:
                if os.getpgid(pid) == managed_group:
                    raise RuntimeError('Unexpected member in protected CMW leader group; drainage unresolved')
            except ProcessLookupError:
                pass
        return members

    def cleanup():
        if managed_foreground:
            # Exclusive foreground topology is checked before launch. CMW retains
            # its leader group; shared birth checks authorize only child groups.
            if managed_members():
                signal_session(managed_owner, signal.SIGTERM, include_leader=False)
            end = time.monotonic() + grace
            while managed_members() and time.monotonic() < end:
                process.poll()
                time.sleep(.05)
            if managed_members():
                signal_session(managed_owner, signal.SIGKILL, include_leader=False)
            process.wait(timeout=grace)
            end = time.monotonic() + grace
            while managed_members() and time.monotonic() < end:
                time.sleep(.05)
            if managed_members():
                raise RuntimeError('Managed descendants remain; completion is unresolved')
            return
        if not group_exists():
            process.wait()
            return
        send_group(signal.SIGTERM)
        end = time.monotonic() + grace
        while time.monotonic() < end:
            process.poll()  # reap the leader even if descendants survive it
            if not group_exists():
                break
            time.sleep(0.05)
        if group_exists():
            send_group(signal.SIGKILL)
        process.wait()

    cleanup_entered = False
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, receive)
        # Install handlers before creating the child: a startup signal cannot orphan it.
        started = time.monotonic()
        start_utc = datetime.now(timezone.utc).isoformat()
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout,
                                   stderr=stderr, start_new_session=not managed_foreground,
                                   preexec_fn=os.setpgrp if managed_foreground else None)
        if control is not None:
            control.register(process.pid, strategy='openmpi')
        if awake:
            inhibitor = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        terminating = False
        while True:
            if control is not None:
                control.poll()
            child = process.poll()
            if requested and not terminating:
                reason = signal.Signals(requested[0]).name
                status = 128 + requested[0]
                if control is None:
                    break
                control.request_termination(reason, exit_code=status)
                terminating = True
            if child is not None and (control.domain_drained() if control is not None else
                                      not managed_foreground or not managed_members()):
                if status is None:
                    status = child if child >= 0 else 128 - child
                break
            elapsed = control.elapsed if control is not None else time.monotonic() - started
            if control is not None:
                if not terminating and (elapsed >= timeout or control.execution_budget_exhausted(execution_budget)):
                    reason, status = ('timeout' if elapsed >= timeout else 'execution-budget'), 124
                    control.request_termination(reason, exit_code=status)
                    terminating = True
                # The worker alone controls computation and termination. A stopped
                # child remains live; runner accounting and hard deadlines continue.
                time.sleep(.05)
                continue
            if elapsed >= timeout:
                reason, status = 'timeout', 124
                break
            if stop_before and stop_note == 'not reached' and elapsed >= timeout - stop_before:
                stop_note = str(on_stop()) if on_stop else 'no stop callback'
            time.sleep(min(0.1, timeout - elapsed))
        if control is None and (reason != 'completed' or (not managed_foreground and group_exists())):
            cleanup_entered = True
            cleanup()
    except BaseException:
        if process is not None and not cleanup_entered:
            if control is None:
                cleanup()
            else:
                control.request_termination('runner-error', exit_code=1)
        raise
    finally:
        if inhibitor is not None:
            inhibitor.terminate()
            try:
                inhibitor.wait(timeout=2)
            except subprocess.TimeoutExpired:
                inhibitor.kill()
                inhibitor.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    result = dict(status=status, child_status=process.returncode, reason=reason,
                start_utc=start_utc, hard_deadline_utc=(datetime.fromisoformat(start_utc)+timedelta(seconds=timeout)).isoformat(), end_utc=datetime.now(timezone.utc).isoformat(),
                elapsed_s=round(control.elapsed if control is not None else time.monotonic() - started, 6), pid=process.pid,
                advance_stop=stop_note)
    if control is not None:
        result.update(suspension_intervals=control.intervals,
                      confirmed_suspended_seconds=control.suspended_seconds,
                      unsuspended_elapsed_seconds=control.unsuspended_elapsed)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--ranks', type=int, default=1)
    parser.add_argument('--mode', choices=['native', 'synthetic'], default='native')
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('program', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    program = args.program[1:] if args.program[:1] == ['--'] else args.program
    if not program:
        parser.error('a program is required after --')
    try:
        command, env = mpi_command(program, args.ranks, args.mode)
        print(f'MPI mode={args.mode} ranks={args.ranks} timeout_s={args.timeout}', file=sys.stderr, flush=True)
        result = supervise(command, args.timeout, env=env)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"MPI exit={result['status']} child={result['child_status']} reason={result['reason']} wall_s={result['elapsed_s']:.3f}", file=sys.stderr)
    return result['status']


if __name__ == '__main__':
    sys.exit(main())
