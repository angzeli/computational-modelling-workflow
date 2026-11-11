"""Bounded test waits: one deadline rule, and a failure that names its condition.

Deadlines are functional bounds, not latency measurements. The predicate is
checked at least once and again when the deadline passes, so a slow final
observation still counts. UI waits that used to count loop passes keep a
wall-clock deadline no shorter than their old nominal time. Event-based UI
barriers live in ui_readiness.py; prefer them when a wait keeps flaking.
"""
import asyncio
import time


def _failure(what, timeout, detail):
    message = f'{what} (bounded wait {timeout:g}s)'
    return AssertionError(message if detail is None else f'{message}; {detail()}')


def wait_for(predicate, *, timeout, what, interval=.05, detail=None):
    """Poll a synchronous predicate until it is truthy, and return its value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise _failure(what, timeout, detail)
        time.sleep(interval)


async def until(predicate, *, timeout, what, pilot=None, interval=.025, detail=None):
    """Await a predicate, pausing through the Textual pilot when one is given."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise _failure(what, timeout, detail)
        if pilot is None:
            await asyncio.sleep(interval)
        else:
            await pilot.pause(interval)
