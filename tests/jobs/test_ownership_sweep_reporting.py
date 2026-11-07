"""Watchdogs stop diagnostic runs; they do not change the lifecycle assertions."""
import json
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests.jobs import ownership_sweep_reporting as reporting


class SweepReportingTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.enterContext(patch.object(reporting, '_write', side_effect=lambda fd, data: self.output.append(data)))
        self.dump = self.enterContext(patch.object(reporting.faulthandler, 'dump_traceback'))
        self.backup = self.enterContext(patch.object(reporting.faulthandler, 'dump_traceback_later'))
        self.cancel_backup = self.enterContext(patch.object(reporting.faulthandler, 'cancel_dump_traceback_later'))
        self.identity = dict(route='controlled', outcome='cancel', point='module:read:123:Store.get#2', fault='EPERM')

    def test_real_alarm_escapes_exception_retry_and_virtual_clock(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(reporting.RowWatchdogExpired):
            with reporting.RowWatchdog(self.identity, wall_seconds=.03, hard_seconds=2):
                with patch.object(time, 'monotonic', return_value=0), patch.object(signal, 'signal'):
                    while True:
                        try:
                            time.sleep(.005)
                        except Exception:
                            continue
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))
        value = json.loads(self.output[0].decode().split(' ', 1)[1])
        self.assertEqual(value['point'], self.identity['point'])
        self.assertEqual(value['fault'], 'EPERM')
        self.assertEqual(value['budget_kind'], 'wall')
        self.assertGreaterEqual(value['wall_elapsed_seconds'], .03)
        self.dump.assert_called_once()
        self.backup.assert_called_once_with(2, file=reporting.sys.__stderr__, exit=True)
        self.cancel_backup.assert_called_once()

    def test_virtual_failure_escapes_subtest_and_names_the_row(self):
        continued = []
        identity = self.identity

        class DiagnosticCase(unittest.TestCase):
            def runTest(self):
                with self.subTest(row=identity):
                    with reporting.RowWatchdog(identity) as guard:
                        guard.fail('virtual', 'virtual budget exceeded', observed=31., limit=30.)
                continued.append(True)

        with self.assertRaises(reporting.RowWatchdogExpired):
            DiagnosticCase().run(unittest.TestResult())
        self.assertEqual(continued, [])
        value = json.loads(self.output[0].decode().split(' ', 1)[1])
        self.assertEqual((value['budget_kind'], value['observed'], value['limit']), ('virtual', 31., 30.))
        self.assertEqual(value['point'], identity['point'])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))

    def test_success_cancels_watchdogs_and_preserves_handler(self):
        previous = signal.getsignal(signal.SIGALRM)
        with reporting.RowWatchdog(self.identity):
            pass
        self.assertEqual(self.output, [])
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))
        self.cancel_backup.assert_called_once()

    def test_existing_timer_is_not_replaced(self):
        with patch.object(reporting, '_get_timer', return_value=(1., 0.)), \
                patch.object(reporting, '_set_timer') as timer, \
                self.assertRaisesRegex(RuntimeError, 'active real timer'):
            with reporting.RowWatchdog(self.identity):
                self.fail('an active timer was replaced')
        timer.assert_not_called()

    def test_native_backup_exits_when_python_alarm_cannot_run(self):
        # Only this disposable diagnostic interpreter ignores SIGALRM. The
        # native fallback must still terminate it and retain the row identity.
        source = '''
import signal, time
from tests.jobs.ownership_sweep_reporting import RowWatchdog, emit
row = dict(route='fixture', outcome='normal', point='blocked-native-read#1', fault='EPERM')
emit('ROW', event='START', **row)
with RowWatchdog(row, wall_seconds=.02, hard_seconds=.05):
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    time.sleep(30)
'''
        result = subprocess.run([sys.executable, '-B', '-c', source],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('blocked-native-read#1', result.stdout)
        self.assertIn('Timeout', result.stderr)

    def test_failfast_keeps_injected_count_and_reports_incomplete_scenario(self):
        from tests.jobs import test_ownership_fault_sweep as sweep
        from tests.jobs.ownership_fault_sweep import ReadPoint
        point = ReadPoint('fixture', 'read', 1, 'read', 1)
        value = dict(clock=0, fired=1, points=[point])
        case = sweep.OwnershipFaultSweepTests('test_group_normal')
        with patch.object(sweep, 'run_lifecycle', return_value=value), \
                patch.object(sweep, 'assert_terminal', side_effect=['terminal', AssertionError('fixture failure')]), \
                patch.object(sweep, 'coverage_manifest', return_value=[]):
            case.setUpClass()
            result = unittest.TestResult()
            result.failfast = True
            case.run(result)
            case.tearDownClass()
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(case.rows), 1)
        self.assertEqual(case.rows[0]['injected_count'], 1)
        self.assertFalse(case.timings[0]['completed'])
        summaries = [json.loads(line.decode().split(' ', 1)[1]) for line in self.output
                     if line.startswith(b'OWNERSHIP_FAULT_SWEEP ')]
        self.assertFalse(summaries[-1]['completed'])

    def test_baseline_failure_reports_incomplete_scenario(self):
        from tests.jobs import test_ownership_fault_sweep as sweep
        case = sweep.OwnershipFaultSweepTests('test_group_normal')
        with patch.object(sweep, 'run_lifecycle', return_value=dict(clock=0, fired=0)), \
                patch.object(sweep, 'assert_terminal', side_effect=AssertionError('fixture baseline failure')):
            case.setUpClass()
            result = unittest.TestResult()
            case.run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(case.timings[0]['completed'])
        self.assertEqual(case.rows, [])
