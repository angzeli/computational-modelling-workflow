"""Wait for the resulting active screen, including dismissal during a refresh."""
from __future__ import annotations

import asyncio
import time

from tests.campaign.navigation_trace import screen_state


async def active_screen_refresh(app, *, expected=lambda: True, timeout=2., records=None):
    """Observe refresh or unmount without leaving a waiter on a removed screen.

    Only the refresh task created here is cancelled. The screen's message task is
    observed, never cancelled. A caller supplies its expected resulting view.
    """
    started = time.monotonic()
    record = {'origin': screen_state(app.screen), 'waited': [], 'deadline_seconds': timeout}
    if records is not None:
        records.append(record)
    try:
        while True:
            remaining = timeout-(time.monotonic()-started)
            if remaining <= 0:
                raise TimeoutError('Resulting active screen refresh deadline exceeded')
            screen = app.screen
            wait = {'screen': screen_state(screen), 'expected': bool(expected())}
            record['waited'].append(wait)
            refresh = asyncio.create_task(screen.wait_for_refresh())
            try:
                done, _ = await asyncio.wait((refresh, screen._task), timeout=remaining,
                                              return_when=asyncio.FIRST_COMPLETED)
                wait['after'] = screen_state(screen)
                wait['current'] = screen_state(app.screen)
                if not done:
                    raise TimeoutError('Resulting active screen refresh deadline exceeded')
                if (refresh in done and refresh.result() and screen is app.screen
                        and screen.is_mounted and screen.is_attached and not screen._layout_required
                        and expected()):
                    record.update(outcome='refreshed', current=screen_state(screen),
                                  elapsed_seconds=time.monotonic()-started)
                    return record
                wait['outcome'] = 'retargeted' if screen is not app.screen else 'not-settled'
            finally:
                if not refresh.done():
                    refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)
    except BaseException as exc:
        record.update(outcome='deadline' if isinstance(exc, TimeoutError) else 'error',
                      error=repr(exc), current=screen_state(app.screen),
                      elapsed_seconds=time.monotonic()-started)
        raise
