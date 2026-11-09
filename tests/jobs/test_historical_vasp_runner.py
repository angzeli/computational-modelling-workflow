"""Qualified VASP runner contract, with the issue #19 historical negative control."""
from contextlib import ExitStack, redirect_stderr
from datetime import datetime
from hashlib import sha1
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, TestResult
from unittest.mock import Mock, patch
import os
import signal
import sys

import psutil
from cmw.jobs import ownership


RUNNER_COMMIT = '3c438192beb19ff9fc209c3fdcba3f5678a20e6e'
RUNNER_BLOBS = {
    # git rev-parse COMMIT:scripts/launch.py in the VASP port repository.
    RUNNER_COMMIT: 'a534d39a522ebe6b9be0a6516d79f87b341fcafd',
    '51d8209': '5a51f958d7c357bec073ccc16725c2bd4df92a5e',
}


def pinned_runner(revision=RUNNER_COMMIT):
    path = Path(__file__).resolve().parents[1] / f'fixtures/vasp_port_{revision[:7]}/launch.py'
    source = path.read_bytes()
    blob = sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest()
    if blob != RUNNER_BLOBS[revision]:
        raise AssertionError(f'launch.py does not match pinned port commit {revision}')
    spec = spec_from_file_location('historical_port_launch', path)
    runner = module_from_spec(spec)
    pins = {'PORT_OPENMPI_VERSION': 'fixture', 'PORT_PRRTE_VERSION': 'fixture'}
    with patch.dict(sys.modules, toolchain=SimpleNamespace(load=lambda: (pins, 'fixture'))):
        spec.loader.exec_module(runner)
    return runner


class HoldObserved(BaseException):
    """Stop the synthetic observation, without claiming the runner completed."""


class FixtureViolation(BaseException):
    """A harness error must never count as an expected historical assertion failure."""


def supervise(runner, *, stepped=False, denied_reads=0, reused=False, timeout=False,
              observed=None):
    """Observe our own kernel PID. Model only the port's topology, child, clocks and send."""
    leader = os.getpid()
    payload, child = 1900000001, 1900000002
    polls = 0
    probes = 0
    now = 0.
    signals = []
    sleeps = []
    waits = []
    diagnostics = StringIO()
    real_read = ownership.process_identity.process_evidence

    def evidence(pid):
        nonlocal probes
        if pid != leader:
            raise FixtureViolation('unexpected PID')
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
            raise FixtureViolation('unexpected domain')
        # A surviving subgroup drains on the next ordinary poll. Real timeout
        # cleanup needs a signal and must exclude both the leader and payload.
        return [leader, payload] + ([child] if polls == 1 and not signals else [])

    def send(owner, signum, *, include_leader, read_domain=None, anchor_ready_for_release=None):
        if owner['pid'] != leader or include_leader:
            raise FixtureViolation('signal outside the permitted fixture subgroup')
        signals.append(signum)

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds
        if now > 70:
            raise HoldObserved()

    def wait(timeout):
        waits.append(timeout)
        return 0

    class Wall(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(1793654971.378596 + (28791.6 if stepped and polls else 0), tz)

    clock = SimpleNamespace(monotonic=lambda: now, sleep=sleep)
    mocked_os = SimpleNamespace(getpgrp=lambda: leader, getpid=lambda: payload,
                                getsid=lambda _: leader,
                                getpgid=lambda pid: leader if pid in (0, leader, payload) else child,
                                environ={'CMW_JOBS_OWN_SESSION': '1'}, setpgrp=lambda: None)
    signal_api = SimpleNamespace(SIGINT=signal.SIGINT, SIGTERM=signal.SIGTERM, SIGKILL=signal.SIGKILL,
                                 Signals=signal.Signals, signal=Mock())
    process = SimpleNamespace(pid=child, returncode=0, poll=poll, wait=wait)
    with ExitStack() as stack:
        stack.enter_context(redirect_stderr(diagnostics))
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
        except HoldObserved:
            return diagnostics.getvalue(), None, signals
        finally:
            if observed is not None:
                observed.update(polls=polls, probes=probes, sleeps=sleeps, waits=waits,
                                elapsed=now, diagnostics=diagnostics.getvalue())


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

    def test_unavailable_exit_reports_a_distinct_outcome_from_gone(self):
        observed = {}
        denied = supervise(self.runner, denied_reads=float('inf'), observed=observed)
        lost = supervise(self.runner, reused=True)
        self.assertNotEqual(denied[0], lost[0])
        self.assertIn('UNVERIFIABLE', denied[0])
        self.assertIn('GONE', lost[0])
        self.assertEqual(denied[1:], (None, []))
        self.assertEqual(lost[1:], (None, []))
        self.assertGreater(observed['elapsed'], 70)
        self.assertGreater(observed['polls'], 1)
        self.assertGreater(observed['probes'], 1)
        self.assertEqual(observed['waits'], [])
        self.assertTrue(all(0 < delay <= 5 for delay in observed['sleeps']))
        self.assertIn('operator attention required', denied[0])

    def test_unavailable_cleanup_identifies_unverifiable_ownership(self):
        observed = {}
        reason, status, sends = supervise(self.runner, timeout=True, denied_reads=float('inf'),
                                          observed=observed)
        self.assertIn('UNVERIFIABLE', reason.upper())
        self.assertEqual((status, sends), (None, []))
        self.assertGreater(observed['elapsed'], 70)
        self.assertGreater(observed['polls'], 1)
        self.assertEqual(observed['waits'], [])
        self.assertIn('operator attention required', reason)

    def test_recovered_whole_check_resumes_ordinary_drainage(self):
        self.assertEqual(supervise(self.runner, denied_reads=2), ('completed', 0, []))

    def test_recovered_whole_check_resumes_ordinary_drainage_across_step(self):
        self.assertEqual(supervise(self.runner, stepped=True, denied_reads=2), ('completed', 0, []))


class HistoricalRunnerNegativeControlTests(TestCase):
    def test_old_pinned_runner_fails_the_four_issue19_assertions(self):
        runner = pinned_runner('51d8209')
        for name in (
            'test_unavailable_exit_reports_a_distinct_outcome_from_gone',
            'test_unavailable_cleanup_identifies_unverifiable_ownership',
            'test_recovered_whole_check_resumes_ordinary_drainage',
            'test_recovered_whole_check_resumes_ordinary_drainage_across_step',
        ):
            with self.subTest(name=name):
                case = HistoricalVaspRunnerTests(name)
                case.runner = runner
                result = TestResult()
                case.run(result)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(result.errors, [])
                self.assertEqual(len(result.failures), 1, 'Historical runner unexpectedly satisfies #19')
                self.assertEqual(result.expectedFailures, [])
