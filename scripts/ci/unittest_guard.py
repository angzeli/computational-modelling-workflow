"""Bound ordinary CI test methods in their existing isolated module process.

The native timeout prints all thread stacks and exits this interpreter. It does
not promise tearDown/cleanup, and never signals another process. The ownership
sweep owns a stricter per-row watchdog and is deliberately exempt here.
The method budget covers setUp, the body, tearDown and instance cleanups;
module/class fixtures remain outside this per-method guard.
"""
import faulthandler
import json
import math
import os
import sys
import unittest


DEFAULT_TIMEOUT_SECONDS = 300.0
SWEEP_MODULE = 'jobs.test_ownership_fault_sweep'
# Test fixtures patch these shared modules. Retain the native functions before
# importing test modules so a stale mock cannot disable its own guard.
_arm = faulthandler.dump_traceback_later
_cancel = faulthandler.cancel_dump_traceback_later
_write = os.write
_stderr = sys.__stderr__


def emit(event, **values):
    _write(1, ('CMW_TEST_CASE_' + event + ' ' + json.dumps(values) + '\n').encode())


def timeout_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('CMW_TEST_TIMEOUT_SECONDS must be a positive finite number') from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('CMW_TEST_TIMEOUT_SECONDS must be a positive finite number')
    return seconds


class GuardedResult(unittest.TextTestResult):
    def __init__(self, *args, budget, **kwargs):
        super().__init__(*args, **kwargs)
        self.budget = budget
        self.guarded = False
        self.completed = 0

    def startTest(self, test):
        super().startTest(test)
        self.guarded = not (
            test.__class__.__module__ in {SWEEP_MODULE, 'tests.' + SWEEP_MODULE}
            and test.__class__.__name__ == 'OwnershipFaultSweepTests')
        emit('STARTED', test=test.id(), tests_started=self.testsRun,
             tests_completed=self.completed,
             timeout_seconds=self.budget if self.guarded else None,
             watchdog='native-per-test' if self.guarded else 'sweep-per-row')
        if self.guarded:
            _arm(self.budget, file=_stderr, exit=True)

    def stopTest(self, test):
        if self.guarded:
            _cancel()
        self.completed += 1
        emit('FINISHED', test=test.id(), tests_started=self.testsRun,
             tests_completed=self.completed)
        super().stopTest(test)


def main(module):
    try:
        budget = timeout_seconds(os.environ.get('CMW_TEST_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS))
    except ValueError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    result = lambda *args, **kwargs: GuardedResult(*args, budget=budget, **kwargs)
    runner = unittest.TextTestRunner(resultclass=result)
    unittest.main(module=None, argv=['python -m unittest', module], testRunner=runner)


if __name__ == '__main__':
    main(sys.argv[1])
