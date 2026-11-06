"""Managed-foreground contract for the VASP port at 51d8209 (cutover issue #19)."""
from contextlib import ExitStack
from datetime import datetime
from hashlib import sha1
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, expectedFailure
from unittest.mock import Mock, patch
import os
import signal
import sys

import psutil
from cmw.jobs import ownership


def pinned_runner():
    path = Path(__file__).resolve().parents[1] / 'fixtures/vasp_port_51d8209/launch.py'
    source = path.read_bytes()
    # git rev-parse 51d8209:scripts/launch.py (also f7afe0a:scripts/launch.py)
    blob = sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest()
    if blob != '5a51f958d7c357bec073ccc16725c2bd4df92a5e':
        raise AssertionError('Historical launch.py does not match the pinned port commit')
    spec = spec_from_file_location('historical_port_launch', path)
    runner = module_from_spec(spec)
    pins = {'PORT_OPENMPI_VERSION': 'fixture', 'PORT_PRRTE_VERSION': 'fixture'}
    with patch.dict(sys.modules, toolchain=SimpleNamespace(load=lambda: (pins, 'fixture'))):
        spec.loader.exec_module(runner)
    return runner


def supervise(runner, *, stepped=False, denied_reads=0, reused=False, timeout=False):
    """Observe our own kernel PID. Model only the port's topology, child, clocks and send."""
    leader = os.getpid()
    payload, child = 1900000001, 1900000002
    polls = 0
    probes = 0
    now = 0.
    signals = []
    real_read = ownership.process_identity.process_evidence

    def evidence(pid):
        nonlocal probes
        if pid != leader:
            raise AssertionError('unexpected PID')
        if polls:
            probes += 1
            if probes <= denied_reads:
                raise PermissionError('synthetic PID read denial')
        token, ended = real_read(pid)
        return token + int(bool(polls and reused)), ended

    def poll():
        nonlocal polls, now
        polls += 1
        now += 0.01
        return None if timeout else 0

    def members(pid, *, session):
        if pid != leader or not session:
            raise AssertionError('unexpected domain')
        # A surviving subgroup drains on the next ordinary poll. Real timeout
        # cleanup needs a signal and must exclude both the leader and payload.
        return [leader, payload] + ([child] if polls == 1 and not signals else [])

    def send(owner, signum, *, include_leader):
        if owner['pid'] != leader or include_leader:
            raise AssertionError('signal outside the permitted fixture subgroup')
        signals.append(signum)

    def sleep(seconds):
        nonlocal now
        now += seconds
        if now > 20:
            raise AssertionError('supervisor did not finish within synthetic time budget')

    class Wall(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(1793654971.378596 + (28791.6 if stepped and polls else 0), tz)

    clock = SimpleNamespace(monotonic=lambda: now, sleep=sleep)
    mocked_os = SimpleNamespace(getpgrp=lambda: leader, getpid=lambda: payload,
                                getsid=lambda _: leader, getpgid=lambda _: child,
                                environ={'CMW_JOBS_OWN_SESSION': '1'}, setpgrp=lambda: None)
    signal_api = SimpleNamespace(SIGINT=signal.SIGINT, SIGTERM=signal.SIGTERM, SIGKILL=signal.SIGKILL,
                                 Signals=signal.Signals, signal=Mock())
    process = SimpleNamespace(pid=child, returncode=0, poll=poll, wait=lambda timeout: 0)
    with ExitStack() as stack:
        def replace(obj, name, value):
            stack.enter_context(patch.object(obj, name, value))
        replace(ownership.process_identity, 'process_evidence', evidence)
        replace(ownership, 'group_members', members)
        replace(ownership, 'signal_session', send)
        replace(ownership, 'time', clock)
        replace(psutil, 'boot_time', lambda: 1793662684.0 if stepped and polls else 1793633892.0)
        replace(psutil.Process, 'create_time', lambda _: 1793683763.378596 if stepped and polls else 1793654971.378596)
        replace(runner, 'os', mocked_os)
        replace(runner, 'time', clock)
        replace(runner, 'datetime', Wall)
        replace(runner, 'signal', signal_api)
        replace(runner, 'subprocess', SimpleNamespace(Popen=lambda *args, **kw: process))
        try:
            result = runner.supervise(['synthetic-only'], .005 if timeout else 3, managed_foreground=True)
            return result['reason'], result['status'], signals
        except RuntimeError as exc:
            return str(exc), None, signals


class HistoricalVaspRunnerTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = pinned_runner()

    def test_ordinary_exit_across_wall_step(self):
        self.assertEqual(supervise(self.runner, stepped=True), ('completed', 0, []))

    def test_real_timeout_across_wall_step(self):
        self.assertEqual(supervise(self.runner, stepped=True, timeout=True), ('timeout', 124, [signal.SIGTERM]))

    def test_single_denied_read_allows_natural_drainage(self):
        for stepped in (False, True):
            with self.subTest(stepped=stepped):
                self.assertEqual(supervise(self.runner, denied_reads=1, stepped=stepped), ('completed', 0, []))

    def test_persistent_denial_and_confirmed_loss_never_send(self):
        for options in ({'denied_reads': float('inf')}, {'denied_reads': float('inf'), 'timeout': True},
                        {'reused': True}):
            with self.subTest(options=options):
                _, status, sends = supervise(self.runner, **options)
                self.assertNotEqual(status, 0)
                self.assertEqual(sends, [])

    # Issue #19. Keep the desired outcomes executable until the successor port is qualified.
    @expectedFailure
    def test_unavailable_exit_reports_a_distinct_outcome_from_gone(self):
        denied = supervise(self.runner, denied_reads=float('inf'))
        lost = supervise(self.runner, reused=True)
        self.assertNotEqual(denied[0], lost[0])

    @expectedFailure
    def test_unavailable_cleanup_identifies_unverifiable_ownership(self):
        reason, _, _ = supervise(self.runner, timeout=True, denied_reads=float('inf'))
        self.assertIn('UNVERIFIABLE', reason.upper())

    @expectedFailure
    def test_recovered_whole_check_resumes_ordinary_drainage(self):
        self.assertEqual(supervise(self.runner, denied_reads=2), ('completed', 0, []))

    @expectedFailure
    def test_recovered_whole_check_resumes_ordinary_drainage_across_step(self):
        self.assertEqual(supervise(self.runner, stepped=True, denied_reads=2), ('completed', 0, []))
