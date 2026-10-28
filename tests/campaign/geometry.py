"""Observe actual terminal layout without driving product refresh or navigation."""
from __future__ import annotations

import asyncio
import os
import time


RESIZE_SEQUENCE = ((140, 40), (100, 30), (77, 24), (80, 24), (140, 40))
SETTLEMENT_SECONDS = 2.0


class GeometryProbe:
    def __init__(self):
        self.layout_generation = 0
        self.previous = None

    def snapshot(self, app):
        from cmw.progress.formatting import cell_width
        from textual.widgets import DataTable, Static

        origin = app.screen_stack[0]
        card = origin.query_one('#progress', Static)
        table = origin.query_one('#table', DataTable)
        shortcuts = origin.query_one('#shortcuts', Static)
        command_bar = origin.query_one('#command-bar')
        content = card.render()
        text = content.plain if hasattr(content, 'plain') else str(content)
        lines = text.splitlines()
        rows = table.ordered_rows
        cursor = rows[table.cursor_row].key.value if rows else None
        selected = (app.selected_key if hasattr(app, 'selected_key') else app._selected_target)
        cursor_identity = app.navigation_row_identity(table, cursor) if cursor else None
        height = table.scrollable_content_region.height - table.header_height
        top = int(table.scroll_y)
        visible = [app.navigation_row_identity(table, row.key.value) for row in rows[top:top + height]]
        painted = origin._compositor.render_strips()
        def crop(region):
            return [strip.crop(region.x, region.right).text for strip in painted[region.y:region.bottom]]
        painted_card = crop(card.region)
        terminal = list(os.get_terminal_size(1)) if os.isatty(1) else None
        result = {
            'terminal_size': terminal, 'app_size': list(app.size), 'screen_size': list(origin.size),
            'modal_open': app.screen is not origin,
            'card_region': list(card.region), 'card_content_region': list(card.content_region),
            'card_widths': [cell_width(line) for line in lines], 'card_lines': len(lines),
            'frame_widths': [cell_width(line) for line in lines if line.startswith(('┌', '│', '├', '└'))],
            'painted_card': painted_card,
            'painted_frame_widths': [cell_width(line.strip()) for line in painted_card
                                     if line.strip().startswith(('┌', '│', '├', '└'))],
            'shortcut_region': list(shortcuts.region),
            'shortcut_strip_visible': shortcuts.display,
            'painted_shortcuts': crop(command_bar.region),
            'table_region': list(table.region), 'table_content_region': list(table.content_region),
            'scroll': [table.scroll_x, table.scroll_y],
            'scroll_target': [table.scroll_target_x, table.scroll_target_y],
            'selected': selected, 'cursor': cursor_identity, 'visible': visible,
            'selected_visible': selected in visible,
            'screen_scroll': [origin.scroll_x, origin.scroll_y],
            'pending_restores': len(app._navigation_restores),
            'pending_restore_tables': [restore.remaining_tables for restore in app._navigation_restores.values()],
            'pending_layout': bool(origin._layout_required),
        }
        # A generation describes observed layout, not a timer or text-age update.
        signature = tuple(repr(result[key]) for key in (
            'app_size', 'screen_size', 'card_region', 'card_content_region', 'card_widths',
            'shortcut_region', 'table_region', 'scroll', 'scroll_target', 'selected', 'cursor'))
        if signature != self.previous:
            self.layout_generation += 1
            self.previous = signature
        result['card_layout_generation'] = self.layout_generation
        return result

    async def settled(self, app, size, *, require_visible=False, timeout=SETTLEMENT_SECONDS):
        started = time.monotonic()
        previous = None
        samples = 0
        while True:
            state = self.snapshot(app)
            samples += 1
            problems = geometry_problems(state, size, require_visible=require_visible)
            generation = state['card_layout_generation']
            if not problems and previous == generation:
                return {**state, 'settlement_seconds': time.monotonic() - started, 'samples': samples}
            previous = generation if not problems else None
            failure = {'geometry_deadline_seconds': timeout, 'problems': problems,
                       'samples': samples, 'last_geometry': state}
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise AssertionError(failure)
            try:
                await asyncio.wait_for(app.screen.wait_for_refresh(), remaining)
            except TimeoutError as exc:
                raise AssertionError(failure) from exc
            await asyncio.sleep(min(.01, remaining))


def geometry_problems(state, size, *, require_visible=False):
    """Independent fit/identity assertions; no parser or formatter as an oracle."""
    problems = []
    size = list(size)
    for name in ('terminal_size', 'app_size', 'screen_size'):
        if state[name] is not None and state[name] != size:
            problems.append(name + ' differs from requested size')
    if state['modal_open']:
        problems.append('base screen is covered')
    expected_width = {max(20, size[0] - 7)}
    if set(state['frame_widths']) != expected_width or set(state['painted_frame_widths']) != expected_width:
        problems.append('card frame retains a different width')
    x, y, width, height = state['card_content_region']
    if max(state['card_widths'], default=0) > width or state['card_lines'] > height:
        problems.append('card content is clipped')
    if x < 0 or y < 0 or x + width > size[0] or y + height > size[1]:
        problems.append('card lies outside terminal')
    if state['shortcut_strip_visible'] and not any(
            'q detach' in ' '.join(line.lower().split()) for line in state['painted_shortcuts']):
        problems.append('Q Detach is clipped')
    if state['pending_restores'] or state['pending_layout'] or state['scroll'] != state['scroll_target']:
        problems.append('layout or navigation restoration is pending')
    if state['selected'] != state['cursor']:
        problems.append('table cursor and immutable selection differ')
    if require_visible and not state['selected_visible']:
        problems.append('explicitly selected target is outside viewport')
    return problems
