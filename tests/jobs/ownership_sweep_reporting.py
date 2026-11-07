"""Real-clock diagnostics outside the sweep's modeled clocks and read hooks."""
import faulthandler
import json
import os
import signal
import sys
import time


WALL_SECONDS = 10.0
HARD_SECONDS = 20.0
# The lifecycle fixtures patch shared time/signal/os modules. Diagnostics must
# neither inherit their virtual clock nor add observations to the injected row.
_clock = time.perf_counter
_write = os.write
_get_timer = signal.getitimer
_set_timer = signal.setitimer
_get_handler = signal.getsignal
_set_handler = signal.signal


def emit(kind, **values):
    _write(1, ('OWNERSHIP_FAULT_' + kind + ' ' + json.dumps(values) + '\n').encode())


class RowWatchdogExpired(KeyboardInterrupt):
    """Escape production retries and unittest.subTest; do not run further rows."""


class RowWatchdog:
    def __init__(self, identity, *, wall_seconds=WALL_SECONDS, hard_seconds=HARD_SECONDS):
        self.identity = identity
        self.wall_seconds, self.hard_seconds = wall_seconds, hard_seconds
        self.model = self.injector = None
        self.started = None

    def capture(self, model, injector):
        self.model, self.injector = model, injector

    def evidence(self):
        trace = self.injector.trace if self.injector is not None else []
        return dict(wall_elapsed_seconds=_clock() - self.started,
                    virtual_elapsed_seconds=self.model.clock if self.model is not None else None,
                    last_observed_site=trace[-1].label() if trace else None,
                    injected_count=self.injector.fired if self.injector is not None else None)

    def fail(self, kind, detail, **values):
        evidence = dict(self.identity, **self.evidence(), budget_kind=kind,
                        detail=detail, **values)
        try:
            emit('WATCHDOG', **evidence)
            faulthandler.dump_traceback(file=sys.__stderr__, all_threads=True)
        finally:
            raise RowWatchdogExpired(json.dumps(evidence))

    def alarm(self, signum, frame):
        self.fail('wall', 'row exceeded real wall-clock budget',
                  budget_seconds=self.wall_seconds)

    def __enter__(self):
        if _get_timer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise RuntimeError('Sweep watchdog refuses to replace an active real timer')
        self.started = _clock()
        self.previous = _get_handler(signal.SIGALRM)
        _set_handler(signal.SIGALRM, self.alarm)
        try:
            # A native watchdog also escapes a C/GIL-blocked call or stuck
            # cleanup. ROW START is already flushed and names the exact row.
            faulthandler.dump_traceback_later(self.hard_seconds, file=sys.__stderr__, exit=True)
            _set_timer(signal.ITIMER_REAL, self.wall_seconds)
        except BaseException:
            faulthandler.cancel_dump_traceback_later()
            _set_handler(signal.SIGALRM, self.previous)
            raise
        return self

    def __exit__(self, *args):
        _set_timer(signal.ITIMER_REAL, 0)
        faulthandler.cancel_dump_traceback_later()
        _set_handler(signal.SIGALRM, self.previous)


clock = _clock
